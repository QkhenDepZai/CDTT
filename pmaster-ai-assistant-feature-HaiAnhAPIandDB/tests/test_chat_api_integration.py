"""
Integration test Giai đoạn 2: Flask /api/chat -> RAG -> Gemini (giả lập) -> MySQL.

Chạy cùng các integration test khác:  PM_TEST_MYSQL=1 DB_PASSWORD=... pytest tests
"""
import json
import os

import pytest

from tests.fakes import FakeEmbeddingService

pytestmark = pytest.mark.skipif(
    os.getenv("PM_TEST_MYSQL") != "1", reason="Đặt PM_TEST_MYSQL=1 để chạy test với MySQL"
)

SITE_HEADERS = {"X-Site-Key": "site_demo_001"}


@pytest.fixture()
def client(test_db, tmp_path, monkeypatch):
    import rag_service
    from knowledge import ingest_service, retriever
    from routes import chat as chat_routes

    fake_embeddings = FakeEmbeddingService()
    kb = retriever.KnowledgeRetriever(embedding_service=fake_embeddings, refresh_seconds=0)
    monkeypatch.setattr(retriever, "_default_retriever", kb)
    monkeypatch.setattr(rag_service, "_default_service", rag_service.RagService(retriever=kb))
    monkeypatch.setattr(ingest_service, "KNOWLEDGE_UPLOAD_FOLDER", str(tmp_path / "kb"))

    # Các bước gọi Gemini phụ (kiểm duyệt, phân loại FAQ) -> giả lập "không vi phạm / không khớp".
    monkeypatch.setattr(chat_routes, "check_violation", lambda text: (False, None))
    monkeypatch.setattr(chat_routes, "find_best_faq_match", lambda cursor, text: None)

    gemini = {"reply": ("Bảng A dành cho học sinh 12-18 tuổi.", "answered"), "contexts": []}

    def fake_create_chat(history, retrieval_context=None):
        gemini["contexts"].append(retrieval_context)
        return object()

    monkeypatch.setattr(rag_service, "create_chat", fake_create_chat)
    monkeypatch.setattr(rag_service, "send_message_with_retry", lambda chat, msg: gemini["reply"])

    ingest_service.ingest_bytes(
        "Bảng A dành cho học sinh từ 12 đến 18 tuổi. Bảng B dành cho sinh viên 19 đến 24 tuổi."
        .encode("utf-8"), "doi_tuong.txt", title="Đối tượng dự thi", embeddings=fake_embeddings,
    )

    from app import app
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client, gemini


def _db_rows(sql, params=()):
    from database import get_db_connection
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    finally:
        connection.close()


def _start_session(test_client):
    response = test_client.post("/api/chat/init", json={}, headers=SITE_HEADERS)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    return body["user_id"], body["conversation_id"]


def test_health(client):
    test_client, _ = client
    response = test_client.get("/api/health")
    assert response.status_code == 200
    assert response.get_json()["database"] == "ok"


def test_chat_requires_site_key(client):
    test_client, _ = client
    assert test_client.post("/api/chat", json={"message": "xin chào"}).status_code == 401


def test_chat_answers_with_rag_sources_and_logs_trace(client):
    test_client, gemini = client
    user_id, conversation_id = _start_session(test_client)

    response = test_client.post("/api/chat", headers=SITE_HEADERS, json={
        "message": "Bảng A dành cho độ tuổi nào?",
        "user_id": user_id, "conversation_id": conversation_id,
    })
    body = response.get_json()

    assert response.status_code == 200, body
    assert body["reply"] == "Bảng A dành cho học sinh 12-18 tuổi."
    assert body["answer_status"] == "answered"
    assert body["sources"][0]["title"] == "Đối tượng dự thi"
    assert "[Nguồn 1] Đối tượng dự thi" in gemini["contexts"][-1]

    rows = _db_rows(
        "SELECT sender_type, answer_status, retrieved_chunk_ids, latency_ms FROM chat_history "
        "WHERE conversation_id = %s AND sender_type = 'model'", (conversation_id,),
    )
    assert rows[-1]["answer_status"] == "answered"
    assert json.loads(rows[-1]["retrieved_chunk_ids"])
    assert rows[-1]["latency_ms"] is not None


def test_three_cannot_answer_escalates_to_staff(client):
    test_client, gemini = client
    gemini["reply"] = ("Mình chưa có thông tin về nội dung này.", "cannot_answer")
    user_id, conversation_id = _start_session(test_client)

    bodies = []
    for question in ["Giải nhất nhận bao nhiêu tiền?", "Có học bổng du học không?",
                     "Ai là trưởng ban giám khảo?"]:
        bodies.append(test_client.post("/api/chat", headers=SITE_HEADERS, json={
            "message": question, "user_id": user_id, "conversation_id": conversation_id,
        }).get_json())

    assert [b.get("fail_count") for b in bodies] == [1, 2, 3]
    assert bodies[-1]["escalated"] is True
    assert bodies[0]["sources"] == []
    status = _db_rows("SELECT status FROM conversations WHERE id = %s", (conversation_id,))
    assert status[0]["status"] == "waiting_agent"
    assert _db_rows("SELECT id FROM agent_notifications WHERE conversation_id = %s",
                    (conversation_id,))
