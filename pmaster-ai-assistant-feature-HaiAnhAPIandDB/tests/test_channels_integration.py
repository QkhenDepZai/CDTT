"""
Integration test Giai đoạn 4: webhook Messenger/Zalo -> dispatcher -> chat_service
-> RAG (Gemini giả lập) -> MySQL -> gửi trả lời (Graph API / Zalo API giả lập
bằng httpx.MockTransport, kiểm tra đúng body HTTP gửi đi).
"""
import hashlib
import hmac
import io
import json
import os

import httpx
import pytest
from PIL import Image

from tests.fakes import FakeEmbeddingService

pytestmark = pytest.mark.skipif(
    os.getenv("PM_TEST_MYSQL") != "1", reason="Đặt PM_TEST_MYSQL=1 để chạy test với MySQL"
)

FB_SECRET = "fb-secret-test"
ZALO_APP_ID = "5550001"
ZALO_OA_SECRET = "zalo-oa-secret"


class FakePlatform:
    """Giả lập Graph API, Zalo API, OAuth Zalo và CDN ảnh."""

    def __init__(self):
        self.requests = []
        self.zalo_token_invalid_once = False
        png = io.BytesIO()
        Image.new("RGB", (40, 20), "white").save(png, format="PNG")
        self.png = png.getvalue()

    def handler(self, request):
        self.requests.append(request)
        host = request.url.host
        if host == "graph.facebook.com":
            return httpx.Response(200, json={"recipient_id": "x", "message_id": "m"})
        if host == "openapi.zalo.me":
            if self.zalo_token_invalid_once and request.headers.get("access_token") == "OLD_TOKEN":
                return httpx.Response(200, json={"error": -216, "message": "Access token is invalid"})
            return httpx.Response(200, json={"error": 0, "message": "Success"})
        if host == "oauth.zaloapp.com":
            return httpx.Response(200, json={"access_token": "NEW_TOKEN",
                                             "refresh_token": "NEW_REFRESH", "expires_in": "90000"})
        if host == "cdn.test":
            return httpx.Response(200, content=self.png, headers={"Content-Type": "image/png"})
        return httpx.Response(404)

    def sent(self, host):
        return [json.loads(r.content) for r in self.requests
                if r.url.host == host and r.method == "POST" and r.content]

    def texts(self, host="graph.facebook.com"):
        return [b["message"]["text"] for b in self.sent(host) if "message" in b]


@pytest.fixture()
def env(test_db, tmp_path, monkeypatch):
    import chat_service
    import image_handler
    import rag_service
    from channels import base, dispatcher, registry, zalo
    from channels.messenger import MessengerAdapter
    from knowledge import ingest_service, retriever

    platform = FakePlatform()
    monkeypatch.setattr(base, "_http_client",
                        httpx.Client(transport=httpx.MockTransport(platform.handler)))

    class Immediate:  # chạy đồng bộ thay cho ThreadPoolExecutor để test tất định
        def submit(self, fn, *args):
            fn(*args)

    monkeypatch.setattr(dispatcher, "_executor", Immediate())

    registry.set_adapter("messenger", MessengerAdapter("PAGE_TOKEN", FB_SECRET, "verify-token"))
    monkeypatch.setattr(zalo, "ZALO_ACCESS_TOKEN", "OLD_TOKEN")
    monkeypatch.setattr(zalo, "ZALO_REFRESH_TOKEN", "OLD_REFRESH")
    monkeypatch.setattr(zalo, "ZALO_APP_SECRET", "zalo-app-secret")
    registry.set_adapter("zalo", zalo.ZaloAdapter(ZALO_APP_ID, ZALO_OA_SECRET,
                                                  zalo.ZaloTokenManager("OA_TEST")))

    fake = FakeEmbeddingService()
    kb = retriever.KnowledgeRetriever(embedding_service=fake, refresh_seconds=0)
    monkeypatch.setattr(retriever, "_default_retriever", kb)
    monkeypatch.setattr(rag_service, "_default_service", rag_service.RagService(retriever=kb))
    monkeypatch.setattr(ingest_service, "KNOWLEDGE_UPLOAD_FOLDER", str(tmp_path / "kb"))
    monkeypatch.setattr(image_handler, "UPLOAD_FOLDER", str(tmp_path / "img"))

    gemini = {"reply": ("**Bảng A** thi 90 phút.", "answered"), "images": 0}

    def send_image(chat, message, data, mime, **kw):
        gemini["images"] += 1
        return ("Ảnh báo lỗi thụt lề.", "answered")

    monkeypatch.setattr(rag_service, "create_chat", lambda history, retrieval_context=None, **kw: None)
    monkeypatch.setattr(rag_service, "send_message_with_retry", lambda chat, msg, **kw: gemini["reply"])
    monkeypatch.setattr(rag_service, "send_message_with_image_retry", send_image)
    monkeypatch.setattr(chat_service, "check_violation", lambda text: (False, None))
    monkeypatch.setattr(chat_service, "find_best_faq_match", lambda cursor, text: None)

    from app import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, platform, gemini
    registry._adapters.clear()


def _post_messenger(client, payload, secret=FB_SECRET):
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/messenger", data=raw, content_type="application/json",
                       headers={"X-Hub-Signature-256": signature})


def _messenger_event(psid, mid, **message):
    return {"object": "page", "entry": [{"id": "PAGE_TEST", "time": 1, "messaging": [
        {"sender": {"id": psid}, "recipient": {"id": "PAGE_TEST"}, "timestamp": 1,
         "message": {"mid": mid, **message}}]}]}


def _post_zalo(client, payload):
    raw = json.dumps(payload).encode()
    base_string = ZALO_APP_ID + raw.decode() + str(payload["timestamp"]) + ZALO_OA_SECRET
    signature = "mac=" + hashlib.sha256(base_string.encode()).hexdigest()
    return client.post("/webhooks/zalo", data=raw, content_type="application/json",
                       headers={"X-ZEvent-Signature": signature})


def _rows(sql, params=()):
    from database import get_db_connection
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    finally:
        connection.close()


# ---- Messenger -------------------------------------------------------------------------
def test_messenger_subscription_verification(env):
    client, _, _ = env
    ok = client.get("/webhooks/messenger?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=987")
    assert ok.status_code == 200 and ok.get_data(as_text=True) == "987"
    bad = client.get("/webhooks/messenger?hub.mode=subscribe&hub.verify_token=sai&hub.challenge=987")
    assert bad.status_code == 403


def test_messenger_rejects_bad_signature(env):
    client, platform, _ = env
    response = _post_messenger(client, _messenger_event("PSID_X", "m.bad", text="hi"), secret="sai")
    assert response.status_code == 403
    assert platform.requests == []


def test_messenger_get_started_sends_greeting_with_quick_replies(env):
    client, platform, _ = env
    payload = {"object": "page", "entry": [{"id": "PAGE_TEST", "messaging": [
        {"sender": {"id": "PSID_1"}, "recipient": {"id": "PAGE_TEST"}, "timestamp": 5,
         "postback": {"title": "Bắt đầu", "payload": "GET_STARTED", "mid": "pb.1"}}]}]}
    assert _post_messenger(client, payload).status_code == 200

    body = platform.sent("graph.facebook.com")[-1]
    assert body["recipient"] == {"id": "PSID_1"}
    assert "Python Master 2026" in body["message"]["text"]
    assert body["message"]["quick_replies"][-1]["payload"] == "REQUEST_AGENT"


def test_messenger_text_goes_through_rag_once_even_if_redelivered(env):
    client, platform, _ = env
    event = _messenger_event("PSID_2", "m.rag.1", text="Bảng A thi bao lâu?")
    assert _post_messenger(client, event).status_code == 200
    assert _post_messenger(client, event).status_code == 200  # Meta gửi lại cùng mid

    replies = [t for t in platform.texts() if "90 phút" in t]
    assert replies == ["Bảng A thi 90 phút."], "chỉ trả lời 1 lần, Markdown đã được bỏ"
    typing = [b for b in platform.sent("graph.facebook.com") if b.get("sender_action") == "typing_on"]
    assert len(typing) == 1

    conversation = _rows(
        "SELECT c.id, c.channel FROM conversations c JOIN user_channel_identities i "
        "ON i.user_id = c.user_id WHERE i.external_user_id = 'PSID_2'")
    assert len(conversation) == 1 and conversation[0]["channel"] == "messenger"
    history = _rows("SELECT sender_type, answer_status FROM chat_history WHERE conversation_id = %s "
                    "ORDER BY message_id", (conversation[0]["id"],))
    assert [h["sender_type"] for h in history] == ["user", "model"]
    assert history[-1]["answer_status"] == "answered"


def test_messenger_cannot_answer_offers_agent_button(env):
    client, platform, gemini = env
    gemini["reply"] = ("Mình chưa có thông tin này.", "cannot_answer")
    _post_messenger(client, _messenger_event("PSID_3", "m.ca.1", text="Giải nhất bao nhiêu tiền?"))
    body = platform.sent("graph.facebook.com")[-1]
    assert body["message"]["quick_replies"] == [
        {"content_type": "text", "title": "Gặp tư vấn viên", "payload": "REQUEST_AGENT"}]


def test_messenger_image_is_downloaded_and_analyzed(env):
    client, platform, gemini = env
    event = _messenger_event("PSID_4", "m.img.1", attachments=[
        {"type": "image", "payload": {"url": "https://cdn.test/loi.png"}}])
    _post_messenger(client, event)
    assert gemini["images"] == 1
    assert platform.texts()[-1] == "Ảnh báo lỗi thụt lề."
    stored = _rows("SELECT image_url FROM messages WHERE image_url IS NOT NULL ORDER BY id DESC LIMIT 1")
    assert stored[0]["image_url"].endswith(".png")


def test_handover_request_and_staff_reply_is_pushed_to_messenger(env):
    client, platform, _ = env
    _post_messenger(client, _messenger_event("PSID_5", "m.ho.1", text="Cho mình gặp tư vấn viên"))
    assert "tư vấn viên" in platform.texts()[-1]

    conversation = _rows(
        "SELECT c.id, c.status FROM conversations c JOIN user_channel_identities i "
        "ON i.user_id = c.user_id WHERE i.external_user_id = 'PSID_5'")[0]
    assert conversation["status"] == "waiting_agent"
    assert _rows("SELECT id FROM agent_notifications WHERE conversation_id = %s", (conversation["id"],))

    # Khi đang chờ tư vấn viên, bot không trả lời bằng AI nữa.
    _post_messenger(client, _messenger_event("PSID_5", "m.ho.2", text="Bảng A thi bao lâu?"))
    assert platform.texts()[-1] == "Yêu cầu của bạn đang chờ tư vấn viên."

    staff_id = _rows("SELECT id FROM users WHERE role = 'staff' LIMIT 1")[0]["id"]
    headers = {"X-Staff-Id": str(staff_id)}
    client.post(f"/api/staff/conversations/{conversation['id']}/claim", json={"staff_id": staff_id},
                headers=headers)
    reply = client.post(f"/api/staff/conversations/{conversation['id']}/reply", headers=headers,
                        json={"staff_id": staff_id, "message": "Chào bạn, mình là tư vấn viên."})
    assert reply.get_json()["channel_delivered"] is True
    last = platform.sent("graph.facebook.com")[-1]
    assert last["recipient"] == {"id": "PSID_5"}
    assert last["message"]["text"] == "Chào bạn, mình là tư vấn viên."


def test_staff_reply_on_web_conversation_is_not_pushed(env):
    client, platform, _ = env
    init = client.post("/api/chat/init", json={}, headers={"X-Site-Key": "site_demo_001"}).get_json()
    client.post("/api/chat/request-agent", json={"conversation_id": init["conversation_id"]},
                headers={"X-Site-Key": "site_demo_001"})
    staff_id = _rows("SELECT id FROM users WHERE role = 'staff' LIMIT 1")[0]["id"]
    before = len(platform.requests)
    reply = client.post(f"/api/staff/conversations/{init['conversation_id']}/reply",
                        headers={"X-Staff-Id": str(staff_id)},
                        json={"staff_id": staff_id, "message": "Chào bạn"})
    assert reply.get_json()["channel_delivered"] is None
    assert len(platform.requests) == before


# ---- Zalo ------------------------------------------------------------------------------------
def _zalo_text(user_id, msg_id, text, timestamp="1700000000001"):
    return {"app_id": ZALO_APP_ID, "user_id_by_app": "u", "event_name": "user_send_text",
            "timestamp": timestamp, "sender": {"id": user_id}, "recipient": {"id": "OA_TEST"},
            "message": {"text": text, "msg_id": msg_id}}


def test_zalo_rejects_bad_signature(env):
    client, _, _ = env
    raw = json.dumps(_zalo_text("Z1", "z.bad", "hi")).encode()
    response = client.post("/webhooks/zalo", data=raw, content_type="application/json",
                           headers={"X-ZEvent-Signature": "mac=deadbeef"})
    assert response.status_code == 403


def test_zalo_text_reply_and_token_refresh(env):
    client, platform, _ = env
    platform.zalo_token_invalid_once = True

    assert _post_zalo(client, _zalo_text("Z_USER_1", "z.1", "Bảng A thi bao lâu?")).status_code == 200

    sends = platform.sent("openapi.zalo.me")
    assert sends[-1] == {"recipient": {"user_id": "Z_USER_1"},
                         "message": {"text": "Bảng A thi 90 phút."}}
    tokens_used = [r.headers.get("access_token") for r in platform.requests
                   if r.url.host == "openapi.zalo.me"]
    assert tokens_used == ["OLD_TOKEN", "NEW_TOKEN"], "-216 -> làm mới token -> gửi lại"

    stored = _rows("SELECT access_token, refresh_token, expires_at FROM channel_tokens "
                   "WHERE channel = 'zalo' AND channel_account_id = 'OA_TEST'")[0]
    assert (stored["access_token"], stored["refresh_token"]) == ("NEW_TOKEN", "NEW_REFRESH")
    assert stored["expires_at"] is not None


def test_zalo_follow_sends_greeting_with_text_suggestions(env):
    client, platform, _ = env
    payload = {"app_id": ZALO_APP_ID, "event_name": "follow", "timestamp": "1700000000002",
               "oa_id": "OA_TEST", "follower": {"id": "Z_USER_2"}}
    assert _post_zalo(client, payload).status_code == 200
    text = platform.sent("openapi.zalo.me")[-1]["message"]["text"]
    assert "Python Master 2026" in text and "• Gặp tư vấn viên" in text


def test_health_reports_channels(env):
    client, _, _ = env
    channels = client.get("/api/health").get_json()["channels"]
    assert channels == {"web": True, "messenger": True, "zalo": True}
