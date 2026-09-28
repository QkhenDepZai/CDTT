"""
Integration test toàn luồng Giai đoạn 1 trên MySQL THẬT:
file -> loader -> chunker -> embedding (giả lập) -> MySQL -> vector index -> search.

Chạy:  PM_TEST_MYSQL=1 DB_HOST=127.0.0.1 DB_USER=root DB_PASSWORD=... pytest tests -v
Test tự tạo/xoá database riêng (PM_TEST_DB_NAME, mặc định gemini_chat_db_test).
"""
import hashlib
import os
import re

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("PM_TEST_MYSQL") != "1", reason="Đặt PM_TEST_MYSQL=1 để chạy test với MySQL"
)

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sql", "01_init_schema.sql")


class FakeEmbeddingService:
    """Embedding giả lập tất định: bag-of-words băm vào 768 chiều.
    Câu hỏi có nhiều từ chung với chunk -> cosine cao, đủ để kiểm chứng luồng
    truy vấn mà không tốn quota Gemini."""

    model = "fake-embedding"
    dimension = 768

    def __init__(self, fail_queries=False):
        self.fail_queries = fail_queries
        self.document_calls = 0

    def _vector(self, text):
        vector = np.zeros(self.dimension, dtype=np.float32)
        for word in re.findall(r"\w+", text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimension] += 1.0
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector

    def embed_documents(self, texts, title=None):
        self.document_calls += 1
        return np.vstack([self._vector(text) for text in texts])

    def embed_query(self, text):
        from knowledge.embedding_service import EmbeddingError
        if self.fail_queries:
            raise EmbeddingError("quota exceeded", 429)
        return self._vector(text)


@pytest.fixture(scope="module")
def test_db():
    import pymysql
    from config import DB_CONFIG

    db_name = DB_CONFIG["database"]
    assert db_name.endswith("_test"), "Chỉ chạy integration test trên database *_test"

    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        script = handle.read().replace("gemini_chat_db", db_name)
    script = "\n".join(line for line in script.splitlines() if not line.strip().startswith("--"))

    admin = pymysql.connect(host=DB_CONFIG["host"], user=DB_CONFIG["user"],
                            password=DB_CONFIG["password"], autocommit=True)
    with admin.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        for statement in script.split(";"):
            if statement.strip():
                cursor.execute(statement)
    yield db_name
    with admin.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    admin.close()


@pytest.fixture()
def services(test_db, tmp_path, monkeypatch):
    from knowledge import ingest_service, retriever

    monkeypatch.setattr(ingest_service, "KNOWLEDGE_UPLOAD_FOLDER", str(tmp_path / "kb"))
    fake = FakeEmbeddingService()
    kb_retriever = retriever.KnowledgeRetriever(embedding_service=fake, refresh_seconds=0)
    monkeypatch.setattr(retriever, "_default_retriever", kb_retriever)
    return ingest_service, kb_retriever, fake


def test_ingest_search_duplicate_and_deactivate(services, tmp_path):
    ingest_service, kb_retriever, fake = services

    rules = tmp_path / "the_le.txt"
    rules.write_text(
        "Bảng A dành cho học sinh từ 12 đến 18 tuổi, thi trong 90 phút.\n\n"
        "Bảng B dành cho sinh viên từ 19 đến 24 tuổi, thi trong 120 phút.\n\n"
        "Lệ phí đăng ký dự thi: miễn phí cho tất cả thí sinh.",
        encoding="utf-8",
    )
    result = ingest_service.ingest_file(str(rules), category="the_le", embeddings=fake)
    assert result.status == "created", result.message
    assert result.chunk_count >= 1

    duplicate = ingest_service.ingest_file(str(rules), embeddings=fake)
    assert duplicate.status == "duplicate"
    assert duplicate.document_id == result.document_id

    hits = kb_retriever.search("lệ phí đăng ký dự thi", top_k=3, min_score=0.1)
    assert hits and "miễn phí" in hits[0].content
    assert hits[0].retrieval_method == "vector"
    assert hits[0].title == "the_le"

    assert ingest_service.set_document_active(result.document_id, False)
    assert kb_retriever.search("lệ phí đăng ký dự thi", top_k=3, min_score=0.1) == []
    assert ingest_service.set_document_active(result.document_id, True)
    assert kb_retriever.search("lệ phí đăng ký dự thi", top_k=3, min_score=0.1)


def test_csv_rows_are_cited_by_row_number(services):
    ingest_service, kb_retriever, fake = services
    raw = "Mốc;Thời gian\nMở đăng ký;01/10/2026\nVòng loại;15/11/2026\n".encode("utf-8")
    result = ingest_service.ingest_bytes(raw, "timeline.csv", title="Timeline 2026",
                                         embeddings=fake)
    assert result.status == "created" and result.chunk_count == 2

    hits = kb_retriever.search("vòng loại thời gian", top_k=1, min_score=0.1)
    assert hits[0].citation() == "Timeline 2026 (dòng 3)"


def test_invalid_file_is_rejected_without_db_row(services):
    ingest_service, _, fake = services
    result = ingest_service.ingest_bytes(b"%PDF-1.4 hong", "broken.pdf", embeddings=fake)
    assert result.status == "failed" and result.document_id is None


def test_failed_embedding_marks_document_failed_then_retry_succeeds(services):
    from database import get_db_connection
    from knowledge import repository
    from knowledge.embedding_service import EmbeddingError

    ingest_service, _, fake = services

    class Broken(FakeEmbeddingService):
        def embed_documents(self, texts, title=None):
            raise EmbeddingError("Gemini Embedding API lỗi 429", 429)

    raw = "Giải nhất Bảng A nhận học bổng.".encode("utf-8")
    failed = ingest_service.ingest_bytes(raw, "giai_thuong.txt", embeddings=Broken())
    assert failed.status == "failed" and failed.document_id

    connection = get_db_connection()
    with connection.cursor() as cursor:
        assert repository.get_document(cursor, failed.document_id)["status"] == "failed"
    connection.close()

    retried = ingest_service.ingest_bytes(raw, "giai_thuong.txt", embeddings=fake)
    assert retried.status == "created" and retried.document_id == failed.document_id


def test_sync_faqs_is_incremental(services):
    from database import get_db_connection

    ingest_service, kb_retriever, fake = services
    connection = get_db_connection()
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO faqs (intent, cau_hoi_mau, tra_loi_chuan) VALUES (%s, %s, %s)",
            [
                ("Điều kiện tham gia", "Chưa biết lập trình có được thi không?",
                 "Được, cuộc thi không yêu cầu kinh nghiệm lập trình trước."),
                ("Chứng chỉ", "Thi xong có chứng chỉ COS Pro không?",
                 "Thí sinh đạt điểm chuẩn được cấp chứng chỉ COS Pro."),
            ],
        )
    connection.commit()
    connection.close()

    first = ingest_service.sync_faqs(embeddings=fake)
    assert first.status == "created" and first.chunk_count == 2
    calls = fake.document_calls

    second = ingest_service.sync_faqs(embeddings=fake)
    assert second.status == "unchanged"
    assert fake.document_calls == calls, "FAQ không đổi thì không được gọi embed lại"

    hits = kb_retriever.search("chứng chỉ COS Pro", top_k=1, min_score=0.1)
    assert hits[0].metadata["faq_id"] > 0


def test_fulltext_fallback_when_query_embedding_fails(services, monkeypatch):
    from knowledge import retriever

    ingest_service, _, fake = services
    ingest_service.ingest_bytes("Địa điểm thi chung kết tại Hà Nội.".encode("utf-8"),
                                "dia_diem.txt", embeddings=fake)

    broken = retriever.KnowledgeRetriever(
        embedding_service=FakeEmbeddingService(fail_queries=True), refresh_seconds=0,
    )
    hits = broken.search("thi chung kết ở đâu", top_k=3)
    assert hits and hits[0].retrieval_method == "fulltext"
    assert any("Hà Nội" in hit.content for hit in hits)
