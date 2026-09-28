"""
API lịch sử chat cho người dùng cuối (D1-03).

    GET /api/history/<user_id>                 danh sách tin nhắn (mới nhất trước)
        ?conversation_id=5   chỉ lấy 1 phiên
        ?limit=50            tối đa HISTORY_MAX_PAGE_SIZE
        ?before_id=1234      trang tiếp theo (lấy từ next_before_id)
    GET /api/history/<user_id>/conversations   danh sách phiên chat
    GET /api/history/<user_id>/export?conversation_id=5   tải phiên chat dạng .txt

Bắt buộc user_token (header X-User-Token) khớp user_id: không ai xem được
lịch sử của người khác chỉ bằng cách đoán user_id (D1-13).
"""
import logging

from flask import Blueprint, Response, jsonify, request

from config import HISTORY_MAX_PAGE_SIZE
from database import (
    get_db_connection,
    get_conversation_messages,
    get_user_conversation,
    list_user_conversations,
    list_user_messages,
)
from security import get_request_user_token, verify_user_token

logger = logging.getLogger("pmaster.history")

history_bp = Blueprint('history', __name__)

SENDER_LABELS = {
    "user": "Bạn",
    "model": "Trợ lý AI",
    "bot": "Trợ lý AI",
    "staff": "Tư vấn viên",
    "system": "Hệ thống",
}


def _authorize(user_id):
    """None nếu hợp lệ, ngược lại trả response lỗi."""
    if not verify_user_token(user_id, get_request_user_token()):
        # Cùng 1 thông báo cho "sai token" và "user không tồn tại" để không
        # lộ user_id nào có thật.
        return jsonify({"error": "Không có quyền xem lịch sử này (thiếu hoặc sai user_token)."}), 403
    return None


def _positive_int_arg(name, default=None, maximum=None):
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"Tham số '{name}' phải là số nguyên")
    if value < 1:
        raise ValueError(f"Tham số '{name}' phải >= 1")
    return min(value, maximum) if maximum else value


def _serialize(row):
    row = dict(row)
    if row.get("created_at") is not None:
        row["created_at"] = row["created_at"].isoformat(sep=" ")
    return row


@history_bp.route('/api/history/<int:user_id>', methods=['GET'])
def get_history(user_id):
    denied = _authorize(user_id)
    if denied:
        return denied
    try:
        limit = _positive_int_arg("limit", 50, HISTORY_MAX_PAGE_SIZE)
        before_id = _positive_int_arg("before_id")
        conversation_id = _positive_int_arg("conversation_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            rows = list_user_messages(cursor, user_id, conversation_id, before_id, limit)
    except Exception:  # noqa: BLE001
        logger.exception("[History] Lỗi đọc lịch sử user_id=%s", user_id)
        return jsonify({"error": "Lỗi hệ thống khi đọc lịch sử chat."}), 500
    finally:
        connection.close()

    messages = [_serialize(row) for row in rows]
    return jsonify({
        "status": "success",
        "user_id": user_id,
        "count": len(messages),
        "messages": messages,
        # Còn dữ liệu cũ hơn nếu trang đầy -> client gửi before_id này để lấy tiếp.
        "next_before_id": messages[-1]["message_id"] if len(messages) == limit else None,
    })


@history_bp.route('/api/history/<int:user_id>/conversations', methods=['GET'])
def get_conversations(user_id):
    denied = _authorize(user_id)
    if denied:
        return denied
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversations = list_user_conversations(cursor, user_id)
    except Exception:  # noqa: BLE001
        logger.exception("[History] Lỗi đọc danh sách phiên user_id=%s", user_id)
        return jsonify({"error": "Lỗi hệ thống khi đọc danh sách phiên chat."}), 500
    finally:
        connection.close()
    return jsonify({
        "status": "success",
        "user_id": user_id,
        "conversations": [_serialize(c) for c in conversations],
    })


@history_bp.route('/api/history/<int:user_id>/export', methods=['GET'])
def export_conversation(user_id):
    """Tải 1 phiên chat dạng text (D1-03: "tải xuống nội dung cuộc trò chuyện")."""
    denied = _authorize(user_id)
    if denied:
        return denied
    try:
        conversation_id = _positive_int_arg("conversation_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not conversation_id:
        return jsonify({"error": "Thiếu conversation_id"}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversation = get_user_conversation(cursor, user_id, conversation_id)
            if not conversation:
                return jsonify({"error": "Không tìm thấy phiên chat"}), 404
            messages = get_conversation_messages(cursor, conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("[History] Lỗi xuất phiên %s", conversation_id)
        return jsonify({"error": "Lỗi hệ thống khi xuất lịch sử chat."}), 500
    finally:
        connection.close()

    lines = [
        "LỊCH SỬ TƯ VẤN - TRỢ LÝ AI PYTHON MASTER 2026",
        f"Phiên #{conversation['id']}: {conversation['title']}",
        f"Bắt đầu: {conversation['created_at']}",
        "=" * 60,
    ]
    for message in messages:
        label = SENDER_LABELS.get(message["sender_type"], message["sender_type"])
        content = message["content"] or ""
        if message.get("image_url"):
            content = f"{content} [ảnh: {message['image_url']}]".strip()
        lines.append(f"[{message['created_at']}] {label}:\n{content}\n")

    return Response(
        "\n".join(lines),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="lich_su_chat_{conversation_id}.txt"',
        },
    )
