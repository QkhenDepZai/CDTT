"""
Điều phối tin nhắn đa kênh: webhook -> chống trùng -> hàng đợi -> chat_service
-> gửi trả lời về nền tảng.

Vì sao xử lý NỀN? Messenger/Zalo yêu cầu webhook trả 200 trong vài giây, nếu
không sẽ gửi lại (thậm chí tạm khoá webhook). Gọi RAG + Gemini có thể mất
vài giây, nên route chỉ xác thực + xếp hàng rồi trả 200 ngay.

Thứ tự: tin nhắn của CÙNG 1 người dùng được xử lý tuần tự (khoá theo người
dùng) để câu trả lời không bị đảo thứ tự và không tạo trùng phiên chat.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

from channels import repository
from channels.base import (
    CMD_FAQ_PREFIX,
    CMD_GET_STARTED,
    CMD_REQUEST_AGENT,
    ChannelAdapter,
    ChannelSendError,
    IncomingMessage,
    QuickReply,
    mask,
)
from channels.media import MediaError, download_image
from config import CHANNEL_WORKERS
from database import get_db_connection, get_faq_by_id, get_faq_suggestions, save_message
from image_handler import build_image_url, prepare_image_for_gemini, save_image
from rag_service import ImageInput
import chat_service

logger = logging.getLogger("pmaster.channels.dispatcher")

_executor = ThreadPoolExecutor(max_workers=CHANNEL_WORKERS, thread_name_prefix="channel")
# WeakValueDictionary: khoá của người dùng không còn tin nhắn đang xử lý tự
# được giải phóng, bộ nhớ không phình theo số người dùng.
_user_locks: "weakref.WeakValueDictionary[tuple, threading.Lock]" = weakref.WeakValueDictionary()
_user_locks_guard = threading.Lock()

# Người dùng gõ tay yêu cầu gặp người thật (D1-09), không cần bấm nút.
HUMAN_REQUEST_PATTERN = re.compile(
    r"(gặp|gap|nói chuyện|kết nối|chuyển).{0,20}(tư vấn viên|tu van vien|nhân viên|người thật|admin)",
    re.IGNORECASE,
)
AGENT_BUTTON = QuickReply("Gặp tư vấn viên", CMD_REQUEST_AGENT)
FALLBACK_ERROR_MESSAGE = (
    "Xin lỗi, hệ thống đang gặp sự cố khi xử lý tin nhắn của bạn. "
    "Vui lòng thử lại sau ít phút hoặc chọn \"Gặp tư vấn viên\"."
)
UNSUPPORTED_MESSAGE = "Hiện mình chỉ hỗ trợ tin nhắn chữ và hình ảnh (JPG, PNG, WEBP)."
PURGE_PROBABILITY = 0.01


def _user_lock(key: tuple) -> threading.Lock:
    with _user_locks_guard:
        lock = _user_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _user_locks[key] = lock
        return lock


def _suggestion_buttons(cursor) -> list[QuickReply]:
    buttons = [
        QuickReply(faq["intent"] or f"Câu hỏi {faq['id']}", f"{CMD_FAQ_PREFIX}{faq['id']}")
        for faq in get_faq_suggestions(cursor, limit=5)
    ]
    return buttons + [AGENT_BUTTON]


# ---- tiếp nhận từ webhook ------------------------------------------------------------
def enqueue_events(adapter: ChannelAdapter, events: list[IncomingMessage]) -> int:
    """Chống trùng rồi xếp hàng xử lý nền. Trả về số sự kiện mới được nhận."""
    accepted = 0
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            for event in events:
                if repository.record_inbound_event(cursor, connection, event.channel, event.event_key):
                    _executor.submit(_process_safely, adapter, event)
                    accepted += 1
                else:
                    logger.info("[Dispatcher] Bỏ qua sự kiện trùng %s/%s",
                                event.channel, mask(event.event_key, 8))
            if random.random() < PURGE_PROBABILITY:
                repository.purge_old_inbound_events(cursor, connection)
    finally:
        connection.close()
    return accepted


def _process_safely(adapter: ChannelAdapter, event: IncomingMessage):
    lock = _user_lock((event.channel, event.channel_account_id, event.external_user_id))
    with lock:
        try:
            process_event(adapter, event)
        except Exception:  # noqa: BLE001 - luồng nền: phải bắt mọi lỗi và báo người dùng
            logger.exception("[Dispatcher] Lỗi xử lý sự kiện %s từ %s",
                             event.channel, mask(event.external_user_id))
            _send(adapter, event.external_user_id, FALLBACK_ERROR_MESSAGE, [AGENT_BUTTON])


def _send(adapter, recipient_id, text, quick_replies=None, from_staff=False) -> bool:
    try:
        adapter.send_long_text(recipient_id, text, quick_replies, from_staff=from_staff)
        return True
    except ChannelSendError as exc:
        logger.error("[Dispatcher] Không gửi được tin %s tới %s: %s",
                     adapter.name, mask(recipient_id), exc)
        return False


# ---- xử lý 1 sự kiện ---------------------------------------------------------------------
def process_event(adapter: ChannelAdapter, event: IncomingMessage) -> str | None:
    """Xử lý đồng bộ 1 sự kiện; trả về nội dung đã gửi (phục vụ test/log)."""
    recipient = event.external_user_id
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            user_id, is_new_user = repository.get_or_create_channel_user(
                cursor, connection, event.channel, event.channel_account_id, recipient)
            conversation = repository.get_active_channel_conversation(
                cursor, connection, user_id, event.channel)
            conversation_id = conversation["id"]

            # 1. Chào mừng: người mới / bấm "Bắt đầu" / theo dõi OA (D1-01).
            wants_greeting = (event.command == CMD_GET_STARTED or event.is_new_follower
                              or (is_new_user and not event.text and not event.image_urls))
            if wants_greeting:
                save_message(cursor, connection, conversation_id, 'system',
                             chat_service.GREETING_TEXT)
                reply = chat_service.GREETING_TEXT
                _send(adapter, recipient, reply, _suggestion_buttons(cursor))
                return reply

            # 2. Nút "Gặp tư vấn viên" hoặc gõ tay yêu cầu gặp người thật (D1-09).
            if event.command == CMD_REQUEST_AGENT or HUMAN_REQUEST_PATTERN.search(event.text):
                if conversation["status"] in ("waiting_agent", "agent"):
                    reply = chat_service.WAITING_AGENT_MESSAGE
                else:
                    if event.text:
                        save_message(cursor, connection, conversation_id, 'user', event.text)
                    reply = chat_service.request_agent(cursor, connection, conversation_id).message
                _send(adapter, recipient, reply)
                return reply

            # 3. Chọn FAQ gợi ý (quick reply).
            if event.command and event.command.startswith(CMD_FAQ_PREFIX):
                faq_id = event.command[len(CMD_FAQ_PREFIX):]
                faq = get_faq_by_id(cursor, int(faq_id)) if faq_id.isdigit() else None
                if faq and conversation["status"] not in ("waiting_agent", "agent"):
                    reply = chat_service.answer_faq(cursor, connection, conversation_id, faq)
                    _send(adapter, recipient, reply)
                    return reply
                # FAQ không còn / đang gặp tư vấn viên -> xử lý như tin nhắn thường.

            # 4. Tin nhắn thường (text và/hoặc ảnh) -> lõi chung như web.
            if not event.text and not event.image_urls:
                _send(adapter, recipient, UNSUPPORTED_MESSAGE)
                return UNSUPPORTED_MESSAGE

            adapter.send_typing(recipient)
            image, image_url = None, None
            if event.image_urls:
                try:
                    image_data = download_image(event.image_urls[0])
                except MediaError as exc:
                    _send(adapter, recipient, str(exc))
                    return str(exc)
                relative_path, _ = save_image(image_data["bytes"], image_data["ext"])
                image_url = build_image_url(relative_path)
                image = ImageInput(*prepare_image_for_gemini(image_data["bytes"],
                                                             image_data["mime_type"]))

            payload = chat_service.process_message(
                cursor, connection, conversation=conversation, user_id=user_id,
                text=event.text, image=image, image_url=image_url,
            )
    finally:
        connection.close()

    reply = payload["reply"]
    if payload.get("suggestions"):
        # Câu hỏi làm rõ (D1-06): Messenger hiển thị lựa chọn dạng nút bấm; bấm
        # nút gửi lại đúng chữ trên nút -> lượt sau tự ghép với câu hỏi gốc.
        options = [QuickReply(option, "CLARIFY") for option in payload["suggestions"]]
        _send(adapter, recipient, payload["clarification_question"], options)
        return reply
    # Gợi ý nút "Gặp tư vấn viên" khi AI không trả lời được (D1-09).
    unanswered = payload.get("answer_status") in ("cannot_answer", "out_of_scope")
    buttons = [AGENT_BUTTON] if unanswered and not payload.get("escalated") else None
    _send(adapter, recipient, reply, buttons)
    logger.info("[Dispatcher] %s user=%s status=%s latency_ms=%s", event.channel,
                mask(recipient), payload.get("answer_status") or payload.get("reason") or "ok",
                payload.get("latency_ms"))
    return reply


# ---- tư vấn viên trả lời người dùng trên Messenger/Zalo ----------------------------------------
def deliver_staff_reply(conversation_id: int, text: str) -> bool | None:
    """Đẩy tin tư vấn viên ra đúng kênh. None nếu phiên thuộc kênh web (không
    cần đẩy), True/False = gửi thành công/thất bại."""
    from channels.registry import get_adapter

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            recipient = repository.get_channel_recipient(cursor, conversation_id)
    finally:
        connection.close()
    if not recipient:
        return None

    adapter = get_adapter(recipient["channel"])
    if adapter is None or not adapter.is_configured:
        logger.error("[Dispatcher] Kênh %s chưa cấu hình, không gửi được tin tư vấn viên.",
                     recipient["channel"])
        return False
    return _send(adapter, recipient["external_user_id"], text, from_staff=True)
