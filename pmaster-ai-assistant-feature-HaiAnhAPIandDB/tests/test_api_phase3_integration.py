"""
Integration test Giai đoạn 3: /api/history/* và /api/knowledge/* trên MySQL thật.
Chạy:  PM_TEST_MYSQL=1 DB_PASSWORD=... pytest tests
"""
import io
import os
import time

import pytest

from tests.fakes import FakeEmbeddingService

pytestmark = pytest.mark.skipif(
    os.getenv("PM_TEST_MYSQL") != "1", reason="Đặt PM_TEST_MYSQL=1 để chạy test với MySQL"
)

SITE = {"X-Site-Key": "site_demo_001"}


@pytest.fixture()
def client(test_db, tmp_path, monkeypatch):
    import rag_service
    from config import ADMIN_API_KEY
    from knowledge import ingest_service, retriever
    from routes import chat as chat_routes

    fake = FakeEmbeddingService()
    kb = retriever.KnowledgeRetriever(embedding_service=fake, refresh_seconds=0)
    monkeypatch.setattr(retriever, "_default_retriever", kb)
    monkeypatch.setattr(ingest_service, "get_embedding_service", lambda: fake)
    monkeypatch.setattr(ingest_service, "KNOWLEDGE_UPLOAD_FOLDER", str(tmp_path / "kb"))
    monkeypatch.setattr(rag_service, "_default_service", rag_service.RagService(retriever=kb))
    monkeypatch.setattr(rag_service, "create_chat", lambda history, retrieval_context=None: None)
    monkeypatch.setattr(rag_service, "send_message_with_retry", lambda chat, msg: ("OK", "answered"))
    monkeypatch.setattr(chat_routes, "check_violation", lambda text: (False, None))
    monkeypatch.setattr(chat_routes, "find_best_faq_match", lambda cursor, text: None)

    from app import app
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client, {"X-Admin-Key": ADMIN_API_KEY}


def _new_user(test_client, messages=()):
    body = test_client.post("/api/chat/init", json={}, headers=SITE).get_json()
    for message in messages:
        test_client.post("/api/chat", headers=SITE, json={
            "message": message, "user_id": body["user_id"],
            "conversation_id": body["conversation_id"]})
    return body


# ---- /api/history --------------------------------------------------------------
def test_history_requires_matching_token(client):
    test_client, _ = client
    alice = _new_user(test_client, ["xin chào"])
    bob = _new_user(test_client)

    url = f"/api/history/{alice['user_id']}"
    assert test_client.get(url).status_code == 403
    assert test_client.get(url, headers={"X-User-Token": bob["user_token"]}).status_code == 403
    assert test_client.get(url, headers={"X-User-Token": alice["user_token"]}).status_code == 200
    # endpoint cũ cũng đã được bảo vệ
    old = f"/api/chat/history/{alice['user_id']}"
    assert test_client.get(old, headers={**SITE, "X-User-Token": bob["user_token"]}).status_code == 403


def test_history_pagination_newest_first(client):
    test_client, _ = client
    user = _new_user(test_client, ["câu 1", "câu 2", "câu 3"])
    headers = {"X-User-Token": user["user_token"]}
    url = f"/api/history/{user['user_id']}"

    first = test_client.get(f"{url}?limit=3", headers=headers).get_json()
    ids = [m["message_id"] for m in first["messages"]]
    assert ids == sorted(ids, reverse=True) and len(ids) == 3
    assert "retrieved_chunk_ids" not in first["messages"][0]

    second = test_client.get(f"{url}?limit=3&before_id={first['next_before_id']}",
                             headers=headers).get_json()
    assert all(m["message_id"] < first["next_before_id"] for m in second["messages"])
    assert test_client.get(f"{url}?limit=abc", headers=headers).status_code == 400


def test_export_conversation_txt(client):
    test_client, _ = client
    user = _new_user(test_client, ["Bảng A thi mấy phút?"])
    response = test_client.get(
        f"/api/history/{user['user_id']}/export?conversation_id={user['conversation_id']}",
        headers={"X-User-Token": user["user_token"]})
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    assert "Bảng A thi mấy phút?" in response.get_data(as_text=True)


# ---- /api/knowledge ------------------------------------------------------------
def test_knowledge_requires_admin_key(client):
    test_client, _ = client
    assert test_client.get("/api/knowledge").status_code == 401
    assert test_client.get("/api/knowledge", headers={"X-Admin-Key": "sai"}).status_code == 401


def test_upload_created_duplicate_and_invalid(client):
    test_client, admin = client

    def upload(raw, name, **fields):
        data = {"file": (io.BytesIO(raw), name), **fields}
        return test_client.post("/api/knowledge/upload", headers=admin, data=data,
                                content_type="multipart/form-data")

    raw = "Vòng chung kết thi tại Hà Nội.".encode("utf-8")
    created = upload(raw, "chung_ket.txt", category="the_le")
    assert created.status_code == 201, created.get_json()
    body = created.get_json()
    assert body["status"] == "success" and body["result"] == "created"

    duplicate = upload(raw, "ban_sao.txt")
    assert duplicate.status_code == 200 and duplicate.get_json()["result"] == "duplicate"

    invalid = upload(b"MZ\x90\x00", "virus.exe")
    assert invalid.status_code == 422 and invalid.get_json()["status"] == "error"

    missing = test_client.post("/api/knowledge/upload", headers=admin, data={},
                               content_type="multipart/form-data")
    assert missing.status_code == 400


def test_background_upload_then_poll_until_ready(client):
    test_client, admin = client
    response = test_client.post("/api/knowledge/upload", headers=admin, data={
        "file": (io.BytesIO("Lệ phí thi: miễn phí.".encode("utf-8")), "le_phi.txt"),
        "background": "true",
    }, content_type="multipart/form-data")
    assert response.status_code == 202
    document_id = response.get_json()["document_id"]

    status = None
    for _ in range(50):
        status = test_client.get(f"/api/knowledge/{document_id}", headers=admin) \
            .get_json()["document"]["status"]
        if status != "processing":
            break
        time.sleep(0.1)
    assert status == "ready"


def test_text_search_toggle_and_delete(client):
    test_client, admin = client
    created = test_client.post("/api/knowledge/text", headers=admin, json={
        "title": "Chứng chỉ", "content": "Thí sinh đạt điểm chuẩn nhận chứng chỉ COS Pro."})
    assert created.status_code == 201
    document_id = created.get_json()["document_id"]

    search = test_client.post("/api/knowledge/search", headers=admin,
                              json={"query": "chứng chỉ COS Pro", "top_k": 3})
    assert any(r["document_id"] == document_id for r in search.get_json()["results"])

    assert test_client.patch(f"/api/knowledge/{document_id}", headers=admin,
                             json={"is_active": "no"}).status_code == 400
    assert test_client.patch(f"/api/knowledge/{document_id}", headers=admin,
                             json={"is_active": False}).status_code == 200
    search = test_client.post("/api/knowledge/search", headers=admin,
                              json={"query": "chứng chỉ COS Pro", "top_k": 3})
    assert all(r["document_id"] != document_id for r in search.get_json()["results"])

    assert test_client.delete(f"/api/knowledge/{document_id}", headers=admin).status_code == 200
    assert test_client.get(f"/api/knowledge/{document_id}", headers=admin).status_code == 404


def test_unknown_route_returns_json_404(client):
    test_client, _ = client
    response = test_client.get("/api/khong-ton-tai")
    assert response.status_code == 404 and "error" in response.get_json()
