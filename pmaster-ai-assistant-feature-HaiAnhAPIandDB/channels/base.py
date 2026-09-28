"""
Kiểu dữ liệu & giao diện chung cho mọi kênh nhắn tin.

Thêm 1 kênh mới (Telegram, Viber, Crisp...) = viết 1 lớp con ChannelAdapter:
    verify_request()  xác thực chữ ký webhook
    parse_events()    payload của nền tảng -> list[IncomingMessage]
    send_text()       gửi câu trả lời về nền tảng
rồi đăng ký trong channels/registry.py. Không phải sửa chat_service/RAG.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from config import CHANNEL_HTTP_TIMEOUT_SECONDS

logger = logging.getLogger("pmaster.channels")

# Payload lệnh dùng chung cho nút bấm / quick reply của mọi kênh.
CMD_GET_STARTED = "GET_STARTED"
CMD_REQUEST_AGENT = "REQUEST_AGENT"
CMD_FAQ_PREFIX = "FAQ_"


@dataclass
class QuickReply:
    title: str
    payload: str


@dataclass
class IncomingMessage:
    """1 sự kiện người dùng đã chuẩn hoá, không phụ thuộc nền tảng."""
    channel: str                    # messenger | zalo
    channel_account_id: str         # Page ID / OA ID
    external_user_id: str           # PSID / Zalo user_id
    event_key: str                  # khoá chống trùng (mid, msg_id...)
    text: str = ""
    image_urls: list[str] = field(default_factory=list)
    command: str | None = None      # GET_STARTED | REQUEST_AGENT | FAQ_<id>
    is_new_follower: bool = False


class ChannelSendError(Exception):
    """Gửi tin nhắn sang nền tảng thất bại (đã log chi tiết)."""

    def __init__(self, message: str, *, retryable: bool = False, code=None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class ChannelAdapter(ABC):
    name: str = ""
    max_text_length: int = 2000

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Đủ khoá bắt buộc để bật kênh."""

    @abstractmethod
    def verify_request(self, raw_body: bytes, headers, payload: dict | None) -> bool:
        """Xác thực webhook đến đúng từ nền tảng (chữ ký)."""

    @abstractmethod
    def parse_events(self, payload: dict) -> list[IncomingMessage]:
        """Chuyển payload webhook thành danh sách IncomingMessage."""

    @abstractmethod
    def send_text(self, recipient_id: str, text: str,
                  quick_replies: list[QuickReply] | None = None,
                  from_staff: bool = False) -> None:
        """Gửi 1 tin nhắn text (<= max_text_length) tới người dùng."""

    def send_typing(self, recipient_id: str) -> None:
        """Hiển thị "đang soạn tin" (không bắt buộc - mặc định không làm gì)."""

    def send_long_text(self, recipient_id: str, text: str,
                       quick_replies: list[QuickReply] | None = None,
                       from_staff: bool = False) -> None:
        """Chuyển Markdown -> text thường, cắt theo giới hạn nền tảng, gửi lần
        lượt; quick reply chỉ gắn vào tin cuối."""
        parts = split_message(to_plain_text(text), self.max_text_length)
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            self.send_text(recipient_id, part, quick_replies if is_last else None,
                           from_staff=from_staff)


# ---- xử lý văn bản -------------------------------------------------------------
_CODE_FENCE = re.compile(r"^```[\w+-]*\s*$", re.MULTILINE)
_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def to_plain_text(text: str) -> str:
    """Messenger/Zalo không hiển thị Markdown: bỏ ```, **, #, ` nhưng GIỮ
    nguyên nội dung và thụt lề của mã nguồn (D1-05)."""
    text = _CODE_FENCE.sub("", text or "")
    text = _BOLD.sub(r"\2", text)
    text = _HEADING.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_message(text: str, max_length: int) -> list[str]:
    """Cắt tin dài tại ranh giới đoạn/dòng/câu gần nhất, mỗi phần <= max_length."""
    text = (text or "").strip()
    if not text:
        return []
    parts = []
    while len(text) > max_length:
        window = text[:max_length]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
        if cut < max_length // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_length
        parts.append(text[:cut + 1].strip())
        text = text[cut + 1:].strip()
    if text:
        parts.append(text)
    return parts


# ---- HTTP ra ngoài -----------------------------------------------------------------
_http_client: httpx.Client | None = None


def http_client() -> httpx.Client:
    """1 client dùng chung (giữ kết nối keep-alive tới Graph API / Zalo API)."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(timeout=CHANNEL_HTTP_TIMEOUT_SECONDS)
    return _http_client


def mask(value: str, keep: int = 4) -> str:
    """Che bớt ID khi ghi log (không log đầy đủ ID người dùng / token)."""
    value = str(value or "")
    return value[:keep] + "***" if len(value) > keep else "***"
