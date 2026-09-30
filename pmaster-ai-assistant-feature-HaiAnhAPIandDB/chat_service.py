"""
Lõi xử lý hội thoại DÙNG CHUNG cho mọi kênh (Website REST API, Facebook
Messenger, Zalo OA).

Mỗi kênh chỉ lo: xác thực request, đọc payload, map người dùng -> users.id,
gửi câu trả lời về đúng nền tảng. Toàn bộ nghiệp vụ nằm ở đây nên mọi kênh
có hành vi giống hệt nhau:

    phiên đang ở tư vấn viên?          -> chỉ lưu tin nhắn (D1-09)
    guardrails (injection, rò rỉ dữ liệu, từ khoá cấm, ngoài phạm vi)
      + kiểm duyệt Gemini               -> từ chối bằng câu mẫu an toàn (D1-07, D1-08, D1-13)
    hỏi "thuộc bảng nào?"              -> xác định bảng / hỏi lại tuổi (D1-06)
    cụm đa nghĩa ("điểm thi")          -> khử nhập nhằng / hỏi lại (D1-06)
    FAQ khớp trực tiếp                 -> trả lời chuẩn (chỉ với tin nhắn text)
    RAG (Knowledge Base + Gemini)      -> trả lời + nguồn (D1-04, D1-05, D1-10)
    không trả lời được 3 lần           -> chuyển tư vấn viên (D1-09)

Hàm trả về dict "payload" (reply + các cờ), tầng kênh tự quyết định cách
hiển thị (JSON cho web, tin nhắn text + nút bấm cho Messenger/Zalo).
"""
import logging
import re

import disambiguation
import guardrails
import handover
import track_advisor
from config import SUPPORT_CONTACT_TEXT
from conversation_memory import build_history_for_gemini
from database import (
    get_conversation_messages,
    get_pending_clarification,
    increment_fail_count,
    log_violation,
    reset_fail_count,
    save_message,
    set_conversation_status,
)
from faq_matcher import find_best_faq_match
from moderation import check_violation
from rag_service import get_rag_service

logger = logging.getLogger("pmaster.chat_service")

GREETING_TEXT = (
    "Chào bạn! Mình là trợ lý ảo của Python Master 2026. Mình có thể giải đáp về thể lệ, "
    "bảng thi, lịch thi, chứng chỉ COS Pro, giải thưởng, tài khoản ôn luyện và kiến thức "
    "Python cơ bản. Bạn có thể chọn nhanh 1 câu hỏi bên dưới, hoặc gõ câu hỏi của bạn."
)
SAFE_REFUSAL_MESSAGE = guardrails.SAFE_REFUSAL_MESSAGE
WAITING_AGENT_MESSAGE = "Yêu cầu của bạn đang chờ tư vấn viên."
AGENT_HANDLING_MESSAGE = "Tư vấn viên đang xử lý yêu cầu của bạn, vui lòng chờ phản hồi."
AGENT_REQUESTED_MESSAGE = handover.AGENT_REQUESTED_MESSAGE
ESCALATE_MESSAGE = (
    "Hệ thống chưa thể trả lời câu hỏi này sau nhiều lần thử. "
    "Đã chuyển yêu cầu của bạn tới tư vấn viên, vui lòng chờ trong giây lát."
)
CONTACT_GUIDANCE = (
    "Nếu cần hỗ trợ thêm, bạn có thể bấm \"Gặp tư vấn viên\" hoặc liên hệ Ban tổ chức "
    f"({SUPPORT_CONTACT_TEXT})."
)
FAIL_ESCALATE_THRESHOLD = 3
USER_REQUEST_REASON = "Người dùng chủ động yêu cầu gặp tư vấn viên"
AUTO_ESCALATE_REASON = "AI không trả lời được 3 lần liên tiếp"

# Phản hồi theo từng loại vi phạm: cờ trả cho client + tiền tố ghi violation_logs.
_BLOCK_FLAGS = {
    guardrails.PROMPT_INJECTION: {"blocked": True, "reason": "prompt_injection"},
    guardrails.INTERNAL_DATA_REQUEST: {"blocked": True, "reason": "internal_data_request"},
    guardrails.BANNED_KEYWORD: {"violation": True, "reason": "banned_keyword"},
}
_OPTION_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")
MAX_OPTION_CHARS = 80


# ---- tiện ích ---------------------------------------------------------------------------------
def split_clarification(reply: str) -> tuple[str, list[str]]:
    """Tách câu hỏi làm rõ của Gemini thành (câu hỏi, các lựa chọn gạch đầu dòng)."""
    question_lines, options = [], []
    for line in (reply or "").splitlines():
        match = _OPTION_LINE.match(line)
        if match:
            options.append(match.group(1).strip("*_ ")[:MAX_OPTION_CHARS])
        elif line.strip():
            question_lines.append(line.strip())
    return " ".join(question_lines).strip(), options[:4]


def _clarify_payload(question: str, options: list[str]) -> dict:
    reply = "\n".join([question, *(f"- {option}" for option in options)])
    return {
        "reply": reply,
        "answer_status": "clarify",
        "clarification_question": question,
        "suggestions": options,
        "used_faq": False,
    }


def _with_contact_guidance(reply: str) -> str:
    """D1-10: không có dữ liệu -> phải hướng dẫn liên hệ tư vấn viên."""
    if "tư vấn viên" in reply.lower():
        return reply
    return f"{reply}\n\n{CONTACT_GUIDANCE}".strip()


# ---- handover ------------------------------------------------------------------------------------
def request_agent(cursor, connection, conversation_id, reason=USER_REQUEST_REASON):
    """Người dùng bấm "Gặp tư vấn viên". Trả về handover.HandoverResult."""
    return handover.request_handover(cursor, connection, conversation_id, reason)


def apply_ai_result(cursor, connection, conversation_id, model_reply, kind, payload):
    """Cập nhật fail_count / chuyển tư vấn viên theo trạng thái trả về từ Gemini."""
    if kind == "answered":
        reset_fail_count(cursor, connection, conversation_id)
    elif kind == "cannot_answer":
        fail_count = increment_fail_count(cursor, connection, conversation_id)
        payload["fail_count"] = fail_count
        if fail_count >= FAIL_ESCALATE_THRESHOLD:
            result = handover.request_handover(cursor, connection, conversation_id,
                                               AUTO_ESCALATE_REASON)
            payload["escalated"] = True
            if result.after_hours:
                # Không có người trực: bot tiếp tục hỗ trợ, đếm lại từ đầu.
                reset_fail_count(cursor, connection, conversation_id)
                payload["after_hours"] = True
                payload["reply"] = f"{model_reply}\n\n{result.message}"
            else:
                save_message(cursor, connection, conversation_id, 'system', ESCALATE_MESSAGE)
                payload["reply"] = f"{model_reply}\n\n{ESCALATE_MESSAGE}"
    # out_of_scope / clarify: không tính lỗi; rate_limited/server_error...: lỗi hạ tầng
    # tạm thời, cũng không tính vào fail_count.


def answer_faq(cursor, connection, conversation_id, faq):
    """Người dùng chọn 1 FAQ gợi ý (nút bấm) -> trả lời chuẩn, không qua AI."""
    save_message(cursor, connection, conversation_id, 'user', f"[Chọn FAQ] {faq['intent']}")
    save_message(cursor, connection, conversation_id, 'model', faq["tra_loi_chuan"],
                 answer_status='faq')
    reset_fail_count(cursor, connection, conversation_id)
    return faq["tra_loi_chuan"]


# ---- các bước xử lý ------------------------------------------------------------------------------
def _refuse(cursor, connection, conversation, user_id, text, image_url, log_reason):
    conversation_id = conversation["id"]
    save_message(cursor, connection, conversation_id, 'user', text, image_url=image_url)
    log_violation(cursor, connection, conversation_id, user_id, text, log_reason)
    save_message(cursor, connection, conversation_id, 'system', SAFE_REFUSAL_MESSAGE,
                 answer_status='blocked')
    return {"reply": SAFE_REFUSAL_MESSAGE, "answer_status": "blocked"}


def _check_safety(cursor, connection, conversation, user_id, text, image_url):
    """Trả về payload từ chối, hoặc None nếu tin nhắn an toàn."""
    result = guardrails.check_message(text)
    if result.category == guardrails.OUT_OF_SCOPE:
        # Ngoài phạm vi không phải vi phạm: không ghi violation_logs, không tính lỗi.
        conversation_id = conversation["id"]
        save_message(cursor, connection, conversation_id, 'user', text, image_url=image_url)
        save_message(cursor, connection, conversation_id, 'model', SAFE_REFUSAL_MESSAGE,
                     answer_status='out_of_scope')
        return {"reply": SAFE_REFUSAL_MESSAGE, "answer_status": "out_of_scope", "used_faq": False}
    if result.blocked:
        payload = _refuse(cursor, connection, conversation, user_id, text, image_url,
                          f"{result.category.upper()}:{result.detail}")
        return {**payload, **_BLOCK_FLAGS[result.category]}

    is_violation, violation_reason = check_violation(text)
    if is_violation:
        payload = _refuse(cursor, connection, conversation, user_id, text, image_url,
                          f"MODERATION:{violation_reason}")
        return {**payload, "violation": True, "reason": "moderation"}
    return None


def _pending_clarification(cursor, conversation_id):
    try:
        return get_pending_clarification(cursor, conversation_id)
    except Exception:  # noqa: BLE001 - DB chưa chạy migration v2: bỏ qua ngữ cảnh làm rõ
        logger.warning("[ChatService] Không đọc được trạng thái hỏi lại", exc_info=True)
        return None


def _resolve_ambiguity(text, previous):
    resolution = disambiguation.resolve(text, previous_text=previous)
    if resolution.needs_clarification and previous:
        # Đã hỏi lại 1 lần mà vẫn mơ hồ -> không hỏi lại mãi, để RAG + Gemini tự xử lý.
        return disambiguation.Resolution(rule=resolution.rule,
                                         retrieval_query=f"{previous} {text}")
    return resolution


def _answer_with_rag(cursor, connection, conversation_id, text, image, resolution):
    db_messages = get_conversation_messages(cursor, conversation_id)
    history = build_history_for_gemini(cursor, connection, conversation_id, db_messages)
    answer = get_rag_service().answer(
        text, history, image=image, cursor=cursor,
        retrieval_query=resolution.retrieval_query, intent_hint=resolution.intent_hint,
    )

    reply = answer.reply
    payload = {"used_faq": False, "answer_status": answer.status,
               "sources": answer.sources_payload(), "latency_ms": answer.latency_ms}
    if answer.status == "out_of_scope":
        reply = SAFE_REFUSAL_MESSAGE
    elif answer.status == "cannot_answer":
        reply = _with_contact_guidance(reply)
    elif answer.status == "clarify":
        question, options = split_clarification(reply)
        payload.update(clarification_question=question or reply, suggestions=options)

    save_message(
        cursor, connection, conversation_id, 'model', reply,
        answer_status=answer.status, retrieved_chunk_ids=answer.chunk_ids,
        latency_ms=answer.latency_ms,
    )
    payload["reply"] = reply
    apply_ai_result(cursor, connection, conversation_id, reply, answer.status, payload)
    return payload


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
        return {"reply": reply, "handled_by": "agent", "conversation_status": status}

    # 3. Kiểm soát an toàn TRƯỚC khi vào nghiệp vụ (chỉ áp dụng khi có text).
    if text:
        refusal = _check_safety(cursor, connection, conversation, user_id, text, image_url)
        if refusal:
            return refusal

    # 4. Câu hỏi cần làm rõ (D1-06). Chỉ với tin nhắn text thuần.
    advice, resolution = track_advisor.TrackAdvice(), disambiguation.Resolution()
    if text and image is None:
        previous = _pending_clarification(cursor, conversation_id)
        advice = track_advisor.advise(text, previous)
        if not advice.applies:
            resolution = _resolve_ambiguity(text, previous)

    save_message(cursor, connection, conversation_id, 'user', text, image_url=image_url)

    if advice.needs_clarification or resolution.needs_clarification:
        source = advice if advice.needs_clarification else resolution
        payload = _clarify_payload(source.clarification, source.options)
        save_message(cursor, connection, conversation_id, 'model', payload["reply"],
                     answer_status='clarify')
        return payload

    if advice.applies:
        reset_fail_count(cursor, connection, conversation_id)
        save_message(cursor, connection, conversation_id, 'model', advice.reply,
                     answer_status='answered')
        return {"reply": advice.reply, "used_faq": False, "answer_status": "answered",
                "intent": track_advisor.INTENT}

    # 5. FAQ khớp trực tiếp (chỉ với text; FAQ matcher không hiểu ảnh).
    #    BỎ QUA khi câu hỏi chứa cụm đa nghĩa: so khớp từ khoá chính là chỗ dễ
    #    bị bẫy nhất ("điểm thi" -> FAQ "Địa điểm thi"); để RAG + gợi ý xử lý.
    if image is None and text and not resolution.applied:
        faq_row = find_best_faq_match(cursor, text)
        if faq_row:
            reply = faq_row['tra_loi_chuan']
            reset_fail_count(cursor, connection, conversation_id)
            save_message(cursor, connection, conversation_id, 'model', reply, answer_status='faq')
            return {"reply": reply, "used_faq": True, "answer_status": "faq",
                    "intent": faq_row.get("intent")}

    # 6. RAG: Knowledge Base + Gemini.
    return _answer_with_rag(cursor, connection, conversation_id, text, image, resolution)
