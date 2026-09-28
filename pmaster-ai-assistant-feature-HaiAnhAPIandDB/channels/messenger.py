"""
Facebook Messenger Platform (Graph API - Send API).

Webhook:
    GET  /webhooks/messenger  xác minh khi đăng ký (hub.verify_token)
    POST /webhooks/messenger  sự kiện: messages, messaging_postbacks
Chữ ký: X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(App Secret, raw body).
Gửi:    POST https://graph.facebook.com/<ver>/me/messages?access_token=<PAGE TOKEN>

Cấu hình trên Meta for Developers:
    1. App > Messenger > Cài đặt API: gắn Page, tạo Page Access Token.
    2. Webhook: Callback URL = https://<domain>/webhooks/messenger,
       Verify token = FB_VERIFY_TOKEN; đăng ký trường messages, messaging_postbacks.
    3. (Khuyến nghị) Cài nút "Bắt đầu" với payload GET_STARTED qua
       Messenger Profile API để người dùng mới thấy lời chào + FAQ gợi ý.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from channels.base import (
    CMD_GET_STARTED,
    ChannelAdapter,
    ChannelSendError,
    IncomingMessage,
    http_client,
    mask,
)
from config import (
    FB_APP_SECRET,
    FB_GRAPH_API_VERSION,
    FB_PAGE_ACCESS_TOKEN,
    FB_USE_HUMAN_AGENT_TAG,
    FB_VERIFY_TOKEN,
)

logger = logging.getLogger("pmaster.channels.messenger")

MAX_QUICK_REPLIES = 13
MAX_QUICK_REPLY_TITLE = 20
# Mã lỗi Graph API có thể thử lại (rate limit / lỗi tạm thời phía Meta).
RETRYABLE_ERROR_CODES = {1, 2, 4, 17, 32, 613}


class MessengerAdapter(ChannelAdapter):
    name = "messenger"
    max_text_length = 2000

    def __init__(self, page_access_token=FB_PAGE_ACCESS_TOKEN, app_secret=FB_APP_SECRET,
                 verify_token=FB_VERIFY_TOKEN, api_version=FB_GRAPH_API_VERSION):
        self.page_access_token = page_access_token
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.api_url = f"https://graph.facebook.com/{api_version}/me/messages"

    @property
    def is_configured(self) -> bool:
        return bool(self.page_access_token and self.app_secret and self.verify_token)

    # ---- xác thực -------------------------------------------------------------
    def verify_subscription(self, mode: str, token: str, challenge: str) -> str | None:
        """Bước đăng ký webhook: trả challenge nếu verify token khớp."""
        if mode == "subscribe" and token and hmac.compare_digest(token, self.verify_token):
            return challenge
        return None

    def verify_request(self, raw_body: bytes, headers, payload=None) -> bool:
        signature = headers.get("X-Hub-Signature-256", "")
        if not signature.startswith("sha256=") or not self.app_secret:
            return False
        expected = hmac.new(self.app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature[len("sha256="):], expected)

    # ---- đọc sự kiện ----------------------------------------------------------------
    def parse_events(self, payload: dict) -> list[IncomingMessage]:
        if payload.get("object") != "page":
            return []
        events = []
        for entry in payload.get("entry") or []:
            page_id = str(entry.get("id") or "")
            for item in entry.get("messaging") or []:
                event = self._parse_messaging(page_id, item)
                if event:
                    events.append(event)
        return events

    @staticmethod
    def _parse_messaging(page_id: str, item: dict) -> IncomingMessage | None:
        sender_id = str((item.get("sender") or {}).get("id") or "")
        if not sender_id or sender_id == page_id:
            return None

        message = item.get("message")
        if message:
            # is_echo = tin do chính Page gửi đi (bot/tư vấn viên) -> bỏ qua.
            if message.get("is_echo"):
                return None
            quick_reply = (message.get("quick_reply") or {}).get("payload")
            image_urls = [
                (att.get("payload") or {}).get("url")
                for att in message.get("attachments") or []
                if att.get("type") == "image" and (att.get("payload") or {}).get("url")
            ]
            return IncomingMessage(
                channel="messenger",
                channel_account_id=page_id,
                external_user_id=sender_id,
                event_key=str(message.get("mid") or f"{sender_id}:{item.get('timestamp')}"),
                text=(message.get("text") or "").strip(),
                image_urls=image_urls,
                command=quick_reply,
            )

        postback = item.get("postback")
        if postback:
            payload = postback.get("payload") or ""
            return IncomingMessage(
                channel="messenger",
                channel_account_id=page_id,
                external_user_id=sender_id,
                event_key=str(postback.get("mid") or f"postback:{sender_id}:{item.get('timestamp')}"),
                text=postback.get("title") or "",
                command=payload,
                is_new_follower=payload == CMD_GET_STARTED,
            )
        # delivery / read / reaction... không cần xử lý.
        return None

    # ---- gửi tin -------------------------------------------------------------------
    def _post(self, body: dict, recipient_id: str) -> None:
        try:
            response = http_client().post(
                self.api_url, params={"access_token": self.page_access_token}, json=body,
            )
        except httpx.HTTPError as exc:
            logger.warning("[Messenger] Lỗi mạng khi gửi tới %s: %s", mask(recipient_id), exc)
            raise ChannelSendError(f"Lỗi mạng: {type(exc).__name__}", retryable=True) from exc

        if response.status_code == 200:
            return
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        code = error.get("code")
        logger.warning(
            "[Messenger] Gửi thất bại tới %s: http=%s code=%s subcode=%s message=%s",
            mask(recipient_id), response.status_code, code, error.get("error_subcode"),
            error.get("message"),
        )
        raise ChannelSendError(
            f"Graph API lỗi {code}: {error.get('message')}",
            retryable=response.status_code >= 500 or code in RETRYABLE_ERROR_CODES,
            code=code,
        )

    def send_text(self, recipient_id, text, quick_replies=None, from_staff=False):
        message = {"text": text}
        if quick_replies:
            message["quick_replies"] = [
                {"content_type": "text", "title": qr.title[:MAX_QUICK_REPLY_TITLE],
                 "payload": qr.payload}
                for qr in quick_replies[:MAX_QUICK_REPLIES]
            ]
        body = {"recipient": {"id": recipient_id}, "message": message}
        if from_staff and FB_USE_HUMAN_AGENT_TAG:
            body.update(messaging_type="MESSAGE_TAG", tag="HUMAN_AGENT")
        else:
            body["messaging_type"] = "RESPONSE"
        self._post(body, recipient_id)

    def send_typing(self, recipient_id):
        try:
            self._post({"recipient": {"id": recipient_id}, "sender_action": "typing_on"},
                       recipient_id)
        except ChannelSendError:
            pass  # chỉ là hiệu ứng hiển thị, lỗi không ảnh hưởng luồng chính
