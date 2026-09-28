"""
Zalo Official Account (Open API v3 + OAuth v4).

Webhook: POST /webhooks/zalo  sự kiện: user_send_text, user_send_image, follow
Chữ ký:  X-ZEvent-Signature = "mac=" + SHA256(app_id + raw_body + timestamp + OA_SECRET_KEY)
         (nối chuỗi rồi băm, KHÔNG phải HMAC; timestamp lấy trong payload).
Gửi:     POST https://openapi.zalo.me/v3.0/oa/message/cs   header access_token
Token:   access token sống ~25 giờ; làm mới bằng refresh token qua
         POST https://oauth.zaloapp.com/v4/oa/access_token (header secret_key).
         Mỗi lần làm mới Zalo cấp refresh token MỚI -> lưu vào channel_tokens.

LƯU Ý: Zalo trả HTTP 200 cả khi lỗi; lỗi thật nằm ở trường "error" != 0.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading

import httpx

from channels import repository
from channels.base import (
    ChannelAdapter,
    ChannelSendError,
    IncomingMessage,
    http_client,
    mask,
)
from config import (
    ZALO_ACCESS_TOKEN,
    ZALO_APP_ID,
    ZALO_APP_SECRET,
    ZALO_OA_ID,
    ZALO_OA_SECRET_KEY,
    ZALO_REFRESH_TOKEN,
)
from database import get_db_connection

logger = logging.getLogger("pmaster.channels.zalo")

SEND_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"
OAUTH_URL = "https://oauth.zaloapp.com/v4/oa/access_token"
# -216: access token không hợp lệ, -124: access token hết hạn.
TOKEN_ERROR_CODES = {-216, -124}
TOKEN_ACCOUNT_KEY_DEFAULT = "default"


class ZaloTokenManager:
    """Cung cấp access token còn hạn; tự làm mới & lưu DB, an toàn đa luồng."""

    def __init__(self, account_id: str = ZALO_OA_ID or TOKEN_ACCOUNT_KEY_DEFAULT):
        self.account_id = account_id
        self._lock = threading.Lock()

    def _load(self, cursor, connection) -> dict | None:
        token = repository.get_token(cursor, "zalo", self.account_id)
        if token is None and ZALO_ACCESS_TOKEN:
            # Lần chạy đầu: nạp token khởi tạo từ .env vào DB.
            repository.save_token(cursor, connection, "zalo", self.account_id,
                                  ZALO_ACCESS_TOKEN, ZALO_REFRESH_TOKEN or None, None)
            token = repository.get_token(cursor, "zalo", self.account_id)
        return token

    def get_access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            connection = get_db_connection()
            try:
                with connection.cursor() as cursor:
                    token = self._load(cursor, connection)
                    if token is None:
                        raise ChannelSendError("Chưa có Zalo access token (ZALO_ACCESS_TOKEN).")
                    if (force_refresh or token["expiring"]) and token["refresh_token"]:
                        refreshed = self._refresh(token["refresh_token"])
                        if refreshed:
                            repository.save_token(
                                cursor, connection, "zalo", self.account_id,
                                refreshed["access_token"], refreshed.get("refresh_token"),
                                int(refreshed.get("expires_in") or 0) or None,
                            )
                            return refreshed["access_token"]
                    return token["access_token"]
            finally:
                connection.close()

    @staticmethod
    def _refresh(refresh_token: str) -> dict | None:
        try:
            response = http_client().post(
                OAUTH_URL,
                headers={"secret_key": ZALO_APP_SECRET},
                data={"refresh_token": refresh_token, "app_id": ZALO_APP_ID,
                      "grant_type": "refresh_token"},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("[Zalo] Làm mới access token lỗi mạng/định dạng: %s", exc)
            return None
        if not body.get("access_token"):
            logger.error("[Zalo] Làm mới access token thất bại: error=%s %s",
                         body.get("error"), body.get("error_description") or body.get("message"))
            return None
        logger.info("[Zalo] Đã làm mới access token (hết hạn sau %ss).", body.get("expires_in"))
        return body


class ZaloAdapter(ChannelAdapter):
    name = "zalo"
    max_text_length = 2000

    def __init__(self, app_id=ZALO_APP_ID, oa_secret_key=ZALO_OA_SECRET_KEY,
                 token_manager: ZaloTokenManager | None = None):
        self.app_id = app_id
        self.oa_secret_key = oa_secret_key
        self.tokens = token_manager or ZaloTokenManager()

    @property
    def is_configured(self) -> bool:
        return bool(self.app_id and self.oa_secret_key and ZALO_APP_SECRET
                    and (ZALO_ACCESS_TOKEN or ZALO_REFRESH_TOKEN))

    # ---- xác thực ---------------------------------------------------------------
    def verify_request(self, raw_body: bytes, headers, payload=None) -> bool:
        signature = headers.get("X-ZEvent-Signature", "").strip()
        if signature.startswith("mac="):
            signature = signature[len("mac="):]
        if not signature or not self.oa_secret_key or not isinstance(payload, dict):
            return False
        # Sự kiện của app khác gửi nhầm -> từ chối.
        if str(payload.get("app_id") or "") != str(self.app_id):
            return False
        timestamp = str(payload.get("timestamp") or "")
        base = (self.app_id + raw_body.decode("utf-8") + timestamp + self.oa_secret_key)
        expected = hashlib.sha256(base.encode("utf-8")).hexdigest()
        return hmac.compare_digest(signature.lower(), expected)

    # ---- đọc sự kiện -------------------------------------------------------------
    def parse_events(self, payload: dict) -> list[IncomingMessage]:
        event_name = payload.get("event_name")
        oa_id = str((payload.get("recipient") or {}).get("id") or payload.get("oa_id") or "")
        timestamp = str(payload.get("timestamp") or "")

        if event_name in ("user_send_text", "user_send_image"):
            sender_id = str((payload.get("sender") or {}).get("id") or "")
            message = payload.get("message") or {}
            if not sender_id:
                return []
            image_urls = [
                (att.get("payload") or {}).get("url")
                for att in message.get("attachments") or []
                if att.get("type") == "image" and (att.get("payload") or {}).get("url")
            ]
            return [IncomingMessage(
                channel="zalo",
                channel_account_id=oa_id,
                external_user_id=sender_id,
                event_key=str(message.get("msg_id") or f"{event_name}:{sender_id}:{timestamp}"),
                text=(message.get("text") or "").strip(),
                image_urls=image_urls,
            )]

        if event_name == "follow":
            follower_id = str((payload.get("follower") or {}).get("id") or "")
            if not follower_id:
                return []
            return [IncomingMessage(
                channel="zalo",
                channel_account_id=oa_id,
                external_user_id=follower_id,
                event_key=f"follow:{follower_id}:{timestamp}",
                is_new_follower=True,
            )]
        # user_send_sticker, user_seen_message, unfollow... không xử lý.
        return []

    # ---- gửi tin ---------------------------------------------------------------------
    def _send_once(self, access_token: str, body: dict) -> dict:
        try:
            response = http_client().post(SEND_URL, headers={"access_token": access_token}, json=body)
            return response.json() if response.status_code == 200 else {
                "error": response.status_code, "message": f"HTTP {response.status_code}",
                "_retryable": response.status_code >= 500,
            }
        except httpx.HTTPError as exc:
            raise ChannelSendError(f"Lỗi mạng: {type(exc).__name__}", retryable=True) from exc
        except ValueError as exc:
            raise ChannelSendError("Zalo trả về dữ liệu không phải JSON", retryable=True) from exc

    def send_text(self, recipient_id, text, quick_replies=None, from_staff=False):
        # Zalo tin "cs" không có quick reply dạng Messenger -> ghép gợi ý vào nội dung.
        if quick_replies:
            options = "\n".join(f"• {qr.title}" for qr in quick_replies)
            text = f"{text}\n\n{options}"[:self.max_text_length]
        body = {"recipient": {"user_id": recipient_id}, "message": {"text": text}}

        result = self._send_once(self.tokens.get_access_token(), body)
        if result.get("error") in TOKEN_ERROR_CODES:
            # Token bị thu hồi/hết hạn sớm -> làm mới ngay và gửi lại 1 lần.
            result = self._send_once(self.tokens.get_access_token(force_refresh=True), body)
        if result.get("error", 0) != 0:
            logger.warning("[Zalo] Gửi thất bại tới %s: error=%s message=%s",
                           mask(recipient_id), result.get("error"), result.get("message"))
            raise ChannelSendError(f"Zalo API lỗi {result.get('error')}: {result.get('message')}",
                                   retryable=bool(result.get("_retryable")),
                                   code=result.get("error"))
