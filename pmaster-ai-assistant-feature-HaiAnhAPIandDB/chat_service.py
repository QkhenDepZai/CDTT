"""
Lõi xử lý hội thoại DÙNG CHUNG cho mọi kênh (Website REST API, Facebook
Messenger, Zalo OA).

Mỗi kênh chỉ lo: xác thực request, đọc payload, map người dùng -> users.id,
gửi câu trả lời về đúng nền tảng. Toàn bộ nghiệp vụ nằm ở đây nên mọi kênh
có hành vi giống hệt nhau:

    phiên đang ở tư vấn viên?  -> chỉ lưu tin nhắn (D1-09)
    Prompt Injection / xin dữ liệu nội bộ / nội dung vi phạm -> từ chối (D1-07, D1-13)
    FAQ khớp trực tiếp          -> trả lời chuẩn (chỉ với tin nhắn text)
    RAG (Knowledge Base + Gemini) -> trả lời + nguồn (D1-10)
    không trả lời được 3 lần    -> chuyển tư vấn viên (D1-09)

Hàm trả về dict "payload" (reply + các cờ), tầng kênh tự quyết định cách
hiển thị (JSON cho web, tin nhắn text cho Messenger/Zalo).
"""
import logging

from conversation_memory import build_history_for_gemini
from database import (
    create_agent_notification,
    get_conversation_messages,
    increment_fail_count,
    log_violation,
    reset_fail_count,
    save_message,
    set_conversation_status,
)
from faq_matcher import find_best_faq_match
from moderation import check_violation
from rag_service import get_rag_service
from security import (
    INTERNAL_DATA_REQUEST_MESSAGE,
    PROMPT_INJECTION_MESSAGE,
    detect_internal_data_request,
    detect_prompt_injection,
)

logger = logging.getLogger("pmaster.chat_service")

GREETING_TEXT = (
    "Chào bạn! Mình là trợ lý ảo của Python Master 2026. "
    "Bạn có thể chọn nhanh 1 câu hỏi bên dưới, hoặc gõ câu hỏi của bạn."
)
VIOLATION_MESSAGE = "Xin lỗi, nội dung của bạn không phù hợp và đã bị từ chối."
WAITING_AGENT_MESSAGE = "Yêu cầu của bạn đang chờ tư vấn viên."
AGENT_HANDLING_MESSAGE = "Tư vấn viên đang xử lý yêu cầu của bạn, vui lòng chờ phản hồi."
AGENT_REQUESTED_MESSAGE = "Đã chuyển yêu cầu tới tư vấn viên, vui lòng chờ trong giây lát."
ESCALATE_MESSAGE = (
    "Hệ thống chưa thể trả lời câu hỏi này sau nhiều lần thử. "
    "Đã chuyển yêu cầu của bạn tới tư vấn viên, vui lòng chờ trong giây lát."
)
FAIL_ESCALATE_THRESHOLD = 3


def save_rag_reply(cursor, connection, conversation_id, answer):
    """Lưu câu trả lời AI kèm dữ liệu truy vết RAG (chunk đã dùng, độ trễ)."""
    save_message(
        cursor, connection, conversation_id, 'model', answer.reply,
        answer_status=answer.status,
        retrieved_chunk_ids=answer.chunk_ids,
        latency_ms=answer.latency_ms,
    )


def apply_ai_result(cursor, connection, conversation_id, model_reply, kind, payload):
    """Cập nhật fail_count / chuyển tư vấn viên theo trạng thái trả về từ Gemini."""
    if kind == "answered":
        reset_fail_count(cursor, connection, conversation_id)
    elif kind == "cannot_answer":
        fail_count = increment_fail_count(cursor, connection, conversation_id)
        payload["fail_count"] = fail_count
        if fail_count >= FAIL_ESCALATE_THRESHOLD:
            save_message(cursor, connection, conversation_id, 'system', ESCALATE_MESSAGE)
            create_agent_notification(
                cursor, connection, conversation_id,
                "AI không trả lời được 3 lần liên tiếp"
            )
            payload["reply"] = f"{model_reply}\n\n{ESCALATE_MESSAGE}"
            payload["escalated"] = True
    # out_of_scope: không tính lỗi; rate_limited/server_error...: lỗi hạ tầng
    # tạm thời, cũng không tính vào fail_count.


def request_agent(cursor, connection, conversation_id, reason="Người dùng chủ động yêu cầu gặp tư vấn viên"):
    set_conversation_status(cursor, connection, conversation_id, 'waiting_agent')
    save_message(cursor, connection, conversation_id, 'system', "Thí sinh yêu cầu gặp tư vấn viên.")
    create_agent_notification(cursor, connection, conversation_id, reason)
    return AGENT_REQUESTED_MESSAGE


def answer_faq(cursor, connection, conversation_id, faq):
    """Người dùng chọn 1 FAQ gợi ý (nút bấm) -> trả lời chuẩn, không qua AI."""
    save_message(cursor, connection, conversation_id, 'user', f"[Chọn FAQ] {faq['intent']}")
    save_message(cursor, connection, conversation_id, 'model', faq["tra_loi_chuan"],
                 answer_status='faq')
    reset_fail_count(cursor, connection, conversation_id)
    return faq["tra_loi_chuan"]


def _refuse(cursor, connection, conversation, user_id, text, image_url, log_reason, reply):
    conversation_id = conversation["id"]
    save_message(cursor, connection, conversation_id, 'user', text, image_url=image_url)
    log_violation(cursor, connection, conversation_id, user_id, text, log_reason)
    save_message(cursor, connection, conversation_id, 'system', reply, answer_status='blocked')
    return {"reply": reply}


def process_message(cursor, connection, *, conversation, user_id, text, image=None, image_url=None):
    """Xử lý 1 tin nhắn của người dùng trên 1 phiên đã xác định.

    conversation: dict có ít nhất id, status (database.get_or_create_conversation).
    image: rag_service.ImageInput đã resize (hoặc None); image_url: đường dẫn ảnh
    đã lưu (hoặc None). Trả về payload dict, luôn có khoá "reply".
    """
    conversation_id = conversation["id"]
    status = conversation["status"]
    text = (text or "").strip()

    # 1. Phiên đã đóng -> thí sinh quay lại, mở lại cho bot xử lý.
    if status == 'closed':
        set_conversation_status(cursor, connection, conversation_id, 'bot')
        reset_fail_count(cursor, connection, conversation_id)
        status = 'bot'

    # 2. Đang chờ/đang được tư vấn viên xử lý -> bot KHÔNG trả lời, chỉ lưu
    #    tin nhắn để nhân viên xem lại lịch sử.
    if status in ('waiting_agent', 'agent'):
        save_message(cursor, connection, conversation_id, 'user', text, image_url=image_url)
        reply = WAITING_AGENT_MESSAGE if status == 'waiting_agent' else AGENT_HANDLING_MESSAGE
        return {"reply": reply, "handled_by": "agent"}

    # 3. Kiểm soát an toàn TRƯỚC khi vào nghiệp vụ (chỉ áp dụng khi có text).
    if text:
        is_injection, injection_reason = detect_prompt_injection(text)
        if is_injection:
            payload = _refuse(cursor, connection, conversation, user_id, text, image_url,
                              f"PROMPT_INJECTION:{injection_reason}", PROMPT_INJECTION_MESSAGE)
            return {**payload, "blocked": True, "reason": "prompt_injection"}

        is_internal, internal_reason = detect_internal_data_request(text)
        if is_internal:
            payload = _refuse(cursor, connection, conversation, user_id, text, image_url,
                              f"INTERNAL_DATA_REQUEST:{internal_reason}",
                              INTERNAL_DATA_REQUEST_MESSAGE)
            return {**payload, "blocked": True, "reason": "internal_data_request"}

        is_violation, violation_reason = check_violation(text)
        if is_violation:
            payload = _refuse(cursor, connection, conversation, user_id, text, image_url,
                              violation_reason, VIOLATION_MESSAGE)
            return {**payload, "violation": True}

    # 4. Lưu tin nhắn hợp lệ.
    save_message(cursor, connection, conversation_id, 'user', text, image_url=image_url)

    # 5. FAQ khớp trực tiếp (chỉ với text; FAQ matcher không hiểu ảnh).
    if image is None and text:
        faq_row = find_best_faq_match(cursor, text)
        if faq_row:
            reply = faq_row['tra_loi_chuan']
            reset_fail_count(cursor, connection, conversation_id)
            save_message(cursor, connection, conversation_id, 'model', reply, answer_status='faq')
            return {"reply": reply, "used_faq": True}

    # 6. RAG: Knowledge Base + Gemini.
    db_messages = get_conversation_messages(cursor, conversation_id)
    history = build_history_for_gemini(cursor, connection, conversation_id, db_messages)
    answer = get_rag_service().answer(text, history, image=image, cursor=cursor)
    save_rag_reply(cursor, connection, conversation_id, answer)

    payload = {
        "reply": answer.reply,
        "used_faq": False,
        "answer_status": answer.status,
        "sources": answer.sources_payload(),
        "latency_ms": answer.latency_ms,
    }
    apply_ai_result(cursor, connection, conversation_id, answer.reply, answer.status, payload)
    return payload
