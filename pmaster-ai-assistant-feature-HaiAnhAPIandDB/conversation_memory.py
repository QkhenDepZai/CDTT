"""
Quản lý cách dựng `history` gửi cho Gemini.

Bản cũ (routes/chat.py._build_history): gửi TOÀN BỘ lịch sử hội thoại mỗi lần
gọi Gemini -> prompt phình to dần theo số lượt chat, tốn token tuyến tính.

Bản mới, theo đúng mô hình:
    System prompt + Conversation summary + Recent turns + Retrieved context + Câu hỏi hiện tại

- CONVERSATION_RECENT_TURNS lượt gần nhất được gửi NGUYÊN VĂN (config.py).
- Các lượt CŨ hơn được nén thành 1 đoạn tóm tắt ngắn (gemini_client.summarize_history),
  lưu lại trong bảng conversations (cột history_summary / summary_covers_up_to_id -
  xem migration_conversation_summary.sql) để lần sau không phải tóm tắt lại từ đầu,
  chỉ tóm tắt thêm phần mới phát sinh.
- Nếu DB CHƯA chạy migration (cột chưa tồn tại) hoặc bước tóm tắt bị lỗi: tự
  động rơi về chế độ "chỉ dùng recent turns, bỏ qua phần cũ hơn" - vẫn giảm
  được token so với bản cũ, và KHÔNG làm crash luồng chat chính. Vì vậy tính
  năng này an toàn để deploy ngay cả khi chưa kịp chạy migration.
"""
import logging

from config import CONVERSATION_RECENT_TURNS, CONVERSATION_SUMMARY_MIN_OLD_MESSAGES
from database import get_conversation_summary_state, update_conversation_summary
from gemini_client import summarize_history

logger = logging.getLogger("pmaster.conversation_memory")


def _message_to_text(msg):
    """Chuyển 1 dòng message trong DB thành text để đưa vào history/summary gửi
    Gemini. Ảnh cũ chỉ đưa vào dưới dạng placeholder text (không gửi lại bytes
    ảnh cũ, vừa tốn token vừa không cần thiết cho ngữ cảnh hội thoại)."""
    if msg.get('image_url') and not msg['content']:
        return "[Hình ảnh người dùng đã gửi trước đó]"
    if msg.get('image_url') and msg['content']:
        return f"{msg['content']} [kèm ảnh]"
    return msg['content']


def _maybe_get_summary_state(cursor, conversation_id):
    try:
        return get_conversation_summary_state(cursor, conversation_id)
    except Exception as exc:  # noqa: BLE001 - có thể do chưa chạy migration
        logger.info(
            "[ConversationMemory] Chưa đọc được history_summary (có thể chưa chạy "
            "migration_conversation_summary.sql), bỏ qua summary lần này: %s", exc,
        )
        return None


def _maybe_update_summary(cursor, connection, conversation_id, summary_text, covers_up_to_id):
    try:
        update_conversation_summary(cursor, connection, conversation_id, summary_text, covers_up_to_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("[ConversationMemory] Không lưu được history_summary: %s", exc)


def build_history_for_gemini(cursor, connection, conversation_id, db_messages):
    """db_messages: kết quả database.get_conversation_messages() (đã bao gồm
    message hiện tại, được lưu ngay trước khi gọi hàm này). Trả về `history`
    (list dict role/parts) để truyền vào gemini_client.create_chat(); KHÔNG
    bao gồm message hiện tại (message đó được gửi riêng qua send_message)."""
    if not db_messages:
        return []

    history_messages = db_messages[:-1]  # bỏ message hiện tại (chưa gửi cho Gemini)
    if not history_messages:
        return []

    recent = history_messages[-CONVERSATION_RECENT_TURNS:]
    older = (
        history_messages[:-CONVERSATION_RECENT_TURNS]
        if len(history_messages) > CONVERSATION_RECENT_TURNS
        else []
    )

    summary_text = None
    if older:
        state = _maybe_get_summary_state(cursor, conversation_id)
        if state is not None:
            already_covered_id = state["summary_covers_up_to_id"]
            uncovered_older = [m for m in older if m["id"] > already_covered_id]

            if len(uncovered_older) >= CONVERSATION_SUMMARY_MIN_OLD_MESSAGES:
                conversation_text = "\n".join(
                    f"{'Thí sinh' if m['sender_type'] == 'user' else 'Trợ lý'}: {_message_to_text(m)}"
                    for m in uncovered_older
                )
                new_summary, status = summarize_history(
                    conversation_text, previous_summary=state["history_summary"]
                )
                if new_summary:
                    covers_up_to_id = uncovered_older[-1]["id"]
                    _maybe_update_summary(cursor, connection, conversation_id, new_summary, covers_up_to_id)
                    summary_text = new_summary
                else:
                    # Tóm tắt lỗi (status khác 'answered') -> dùng tạm summary cũ,
                    # không chặn luồng chat chính vì 1 bước phụ bị lỗi.
                    summary_text = state["history_summary"]
            else:
                summary_text = state["history_summary"]

    history = []
    if summary_text:
        # Đưa summary vào dưới dạng 1 cặp lượt hội thoại "ảo" ở đầu history,
        # để model hiểu đây là bối cảnh nền, không phải câu hỏi cần trả lời.
        history.append({
            "role": "user",
            "parts": [{"text": f"[Tóm tắt các lượt trò chuyện trước đó]\n{summary_text}"}],
        })
        history.append({
            "role": "model",
            "parts": [{"text": "Đã nắm được bối cảnh trước đó."}],
        })

    for msg in recent:
        role = "user" if msg['sender_type'] == 'user' else "model"
        history.append({"role": role, "parts": [{"text": _message_to_text(msg)}]})

    return history
