import hashlib
import hmac
import json

import httpx
import pytest

from channels import base
from channels.base import CMD_GET_STARTED, QuickReply, split_message, to_plain_text
from channels.messenger import MessengerAdapter
from channels.zalo import ZaloAdapter

SECRET = "fb-app-secret"


def _fb_signature(raw: bytes, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


@pytest.fixture()
def messenger():
    return MessengerAdapter(page_access_token="PAGE_TOKEN", app_secret=SECRET,
                            verify_token="verify-me", api_version="v23.0")


@pytest.fixture()
def captured(monkeypatch):
    """Thay HTTP client dùng chung bằng MockTransport, ghi lại mọi request."""
    requests = []
    responses = {"status": 200, "json": {"message_id": "m1"}}

    def handler(request):
        requests.append(request)
        return httpx.Response(responses["status"], json=responses["json"])

    monkeypatch.setattr(base, "_http_client", httpx.Client(transport=httpx.MockTransport(handler)))
    return requests, responses


# ---- xử lý văn bản -------------------------------------------------------------
def test_to_plain_text_strips_markdown_but_keeps_code():
    text = "## Lỗi\n**IndentationError** do thụt lề.\n```python\nif x:\n    print(x)\n```\nDùng `print()`."
    assert to_plain_text(text) == (
        "Lỗi\nIndentationError do thụt lề.\n\nif x:\n    print(x)\n\nDùng print()."
    )


def test_split_message_respects_limit_and_boundaries():
    text = "\n\n".join(f"Đoạn {i}: " + "nội dung " * 20 for i in range(10))
    parts = split_message(text, 400)
    assert all(len(p) <= 400 for p in parts)
    assert "".join(p.replace(" ", "").replace("\n", "") for p in parts) == \
        text.replace(" ", "").replace("\n", "")


def test_split_message_empty():
    assert split_message("   ", 100) == []


# ---- Messenger: xác thực ----------------------------------------------------------
def test_messenger_signature(messenger):
    raw = b'{"object":"page"}'
    assert messenger.verify_request(raw, {"X-Hub-Signature-256": _fb_signature(raw)})
    assert not messenger.verify_request(raw, {"X-Hub-Signature-256": _fb_signature(raw, "sai")})
    assert not messenger.verify_request(raw + b" ", {"X-Hub-Signature-256": _fb_signature(raw)})
    assert not messenger.verify_request(raw, {})


def test_messenger_subscription(messenger):
    assert messenger.verify_subscription("subscribe", "verify-me", "123") == "123"
    assert messenger.verify_subscription("subscribe", "sai", "123") is None
    assert messenger.verify_subscription("unsubscribe", "verify-me", "123") is None


# ---- Messenger: đọc sự kiện ---------------------------------------------------------
def _entry(*messaging):
    return {"object": "page", "entry": [{"id": "PAGE1", "time": 1, "messaging": list(messaging)}]}


def test_messenger_parse_text_quick_reply_image_postback(messenger):
    payload = _entry(
        {"sender": {"id": "U1"}, "recipient": {"id": "PAGE1"}, "timestamp": 1,
         "message": {"mid": "m.1", "text": " Lệ phí thi? "}},
        {"sender": {"id": "U1"}, "recipient": {"id": "PAGE1"}, "timestamp": 2,
         "message": {"mid": "m.2", "text": "Lịch thi", "quick_reply": {"payload": "FAQ_7"}}},
        {"sender": {"id": "U1"}, "recipient": {"id": "PAGE1"}, "timestamp": 3,
         "message": {"mid": "m.3", "attachments": [
             {"type": "image", "payload": {"url": "https://cdn.fb/x.png"}},
             {"type": "audio", "payload": {"url": "https://cdn.fb/a.mp4"}}]}},
        {"sender": {"id": "U1"}, "recipient": {"id": "PAGE1"}, "timestamp": 4,
         "postback": {"title": "Bắt đầu", "payload": CMD_GET_STARTED}},
    )
    text, quick, image, postback = messenger.parse_events(payload)
    assert (text.text, text.event_key, text.channel_account_id) == ("Lệ phí thi?", "m.1", "PAGE1")
    assert quick.command == "FAQ_7"
    assert image.image_urls == ["https://cdn.fb/x.png"] and image.text == ""
    assert postback.command == CMD_GET_STARTED and postback.is_new_follower


def test_messenger_ignores_echo_delivery_and_other_objects(messenger):
    payload = _entry(
        {"sender": {"id": "PAGE1"}, "recipient": {"id": "U1"},
         "message": {"mid": "m.9", "text": "bot", "is_echo": True}},
        {"sender": {"id": "U1"}, "recipient": {"id": "PAGE1"}, "delivery": {"mids": ["m.1"]}},
    )
    assert messenger.parse_events(payload) == []
    assert messenger.parse_events({"object": "instagram", "entry": []}) == []


# ---- Messenger: gửi tin -----------------------------------------------------------------
def test_messenger_send_text_body(messenger, captured):
    requests, _ = captured
    messenger.send_text("U1", "Xin chào", [QuickReply("Một tiêu đề rất rất dài hơn 20", "FAQ_1")])
    request = requests[0]
    assert request.url.path == "/v23.0/me/messages"
    assert request.url.params["access_token"] == "PAGE_TOKEN"
    body = json.loads(request.content)
    assert body["recipient"] == {"id": "U1"} and body["messaging_type"] == "RESPONSE"
    assert body["message"]["quick_replies"][0]["title"] == "Một tiêu đề rất rất "
    assert len(body["message"]["quick_replies"][0]["title"]) == 20


def test_messenger_long_text_is_split_and_quick_replies_on_last(messenger, captured):
    requests, _ = captured
    messenger.send_long_text("U1", "câu dài. " * 400, [QuickReply("Gặp tư vấn viên", "REQUEST_AGENT")])
    bodies = [json.loads(r.content) for r in requests]
    assert len(bodies) > 1
    assert all(len(b["message"]["text"]) <= 2000 for b in bodies)
    assert "quick_replies" not in bodies[0]["message"] and "quick_replies" in bodies[-1]["message"]


def test_messenger_send_error_is_classified(messenger, captured):
    from channels.base import ChannelSendError

    _, responses = captured
    responses.update(status=400, json={"error": {"code": 613, "message": "rate limit"}})
    with pytest.raises(ChannelSendError) as info:
        messenger.send_text("U1", "hi")
    assert info.value.retryable and info.value.code == 613

    responses.update(status=400, json={"error": {"code": 190, "message": "token hết hạn"}})
    with pytest.raises(ChannelSendError) as info:
        messenger.send_text("U1", "hi")
    assert not info.value.retryable


# ---- Zalo: xác thực & đọc sự kiện -------------------------------------------------------
ZALO_APP_ID = "12345"
ZALO_OA_SECRET = "oa-secret"


def _zalo_signature(raw: bytes, timestamp: str) -> str:
    base_string = ZALO_APP_ID + raw.decode() + timestamp + ZALO_OA_SECRET
    return "mac=" + hashlib.sha256(base_string.encode()).hexdigest()


@pytest.fixture()
def zalo():
    return ZaloAdapter(app_id=ZALO_APP_ID, oa_secret_key=ZALO_OA_SECRET, token_manager=object())


def test_zalo_signature(zalo):
    payload = {"app_id": ZALO_APP_ID, "event_name": "user_send_text", "timestamp": "1700000000000"}
    raw = json.dumps(payload).encode()
    sig = _zalo_signature(raw, "1700000000000")
    assert zalo.verify_request(raw, {"X-ZEvent-Signature": sig}, payload)
    assert zalo.verify_request(raw, {"X-ZEvent-Signature": sig.replace("mac=", "")}, payload)
    assert not zalo.verify_request(raw + b" ", {"X-ZEvent-Signature": sig}, payload)
    other_app = dict(payload, app_id="999")
    assert not zalo.verify_request(raw, {"X-ZEvent-Signature": sig}, other_app)


def test_zalo_parse_events(zalo):
    text = zalo.parse_events({
        "app_id": ZALO_APP_ID, "event_name": "user_send_text", "timestamp": "1",
        "sender": {"id": "Z1"}, "recipient": {"id": "OA1"},
        "message": {"text": "Bảng B thi khi nào?", "msg_id": "z.1"}})[0]
    assert (text.external_user_id, text.channel_account_id, text.event_key) == ("Z1", "OA1", "z.1")

    image = zalo.parse_events({
        "app_id": ZALO_APP_ID, "event_name": "user_send_image", "timestamp": "2",
        "sender": {"id": "Z1"}, "recipient": {"id": "OA1"},
        "message": {"msg_id": "z.2", "attachments": [
            {"type": "image", "payload": {"url": "https://zalo.cdn/i.jpg", "thumbnail": "t"}}]}})[0]
    assert image.image_urls == ["https://zalo.cdn/i.jpg"]

    follow = zalo.parse_events({"app_id": ZALO_APP_ID, "event_name": "follow", "timestamp": "3",
                                "oa_id": "OA1", "follower": {"id": "Z2"}})[0]
    assert follow.is_new_follower and follow.external_user_id == "Z2"

    assert zalo.parse_events({"event_name": "user_send_sticker", "sender": {"id": "Z1"}}) == []
