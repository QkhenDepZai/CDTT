"""
API cho tư vấn viên (D1-09) - dùng bởi Staff Dashboard (/staff).

    GET  /api/staff/conversations?scope=waiting|mine|open   danh sách phiên
    GET  /api/staff/conversations/<id>/messages             lịch sử đầy đủ
    POST /api/staff/conversations/<id>/claim                tiếp nhận phiên chờ
    POST /api/staff/conversations/<id>/reply                trả lời thí sinh
    POST /api/staff/conversations/<id>/close                đóng phiên
    GET  /api/staff/notifications                           thông báo chưa đọc
    POST /api/staff/notifications/<id>/read
    GET  /api/staff/tickets?status=open|in_progress|resolved
    POST /api/staff/tickets/<id>/status                     {"status": "..."}

Xác thực: staff_id (query/body hoặc header X-Staff-Id) phải có role 'staff'.
Giới hạn đã biết: chưa có mật khẩu cho tư vấn viên - xem create_staff.py.
"""
import logging
from functools import wraps

from flask import Blueprint, jsonify, request

from channels.dispatcher import deliver_staff_reply
from database import (
    STAFF_SCOPES,
    TICKET_STATUSES,
    assign_staff,
    close_conversation,
    get_conversation,
    get_conversation_messages,
    get_db_connection,
    get_support_ticket,
    get_user_role,
    list_support_tickets,
    list_unread_agent_notifications,
    list_waiting_conversations,
    mark_agent_notification_read,
    save_message,
    update_support_ticket_status,
)

staff_bp = Blueprint('staff', __name__)
logger = logging.getLogger("pmaster.routes.staff")

CLAIM_MESSAGE = "Tư vấn viên đã tiếp nhận phiên chat."
CLOSE_MESSAGE = "Tư vấn viên đã đánh dấu hoàn thành, phiên chat đã đóng."


def _deliver(conversation_id, text):
    """Đẩy tin ra Messenger/Zalo nếu phiên thuộc các kênh đó. None = phiên web
    (widget tự lấy tin qua API); False = gửi thất bại (đã log)."""
    try:
        return deliver_staff_reply(conversation_id, text)
    except Exception:  # noqa: BLE001 - tin đã lưu DB, lỗi gửi kênh không làm hỏng request
        logger.exception("[Staff] Không đẩy được tin ra kênh cho phiên %s", conversation_id)
        return False


def _request_staff_id():
    data = request.get_json(silent=True) or {}
    return (data.get("staff_id") or request.args.get("staff_id")
            or request.headers.get("X-Staff-Id"))


def staff_endpoint(view):
    """Kiểm tra quyền tư vấn viên, mở kết nối DB và truyền (cursor, connection,
    staff_id) cho view; lỗi không lường trước trả 500 chung chung (chi tiết ở log)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        raw_staff_id = _request_staff_id()
        if not raw_staff_id:
            return jsonify({"error": "Thiếu staff_id"}), 400
        try:
            staff_id = int(raw_staff_id)
        except (TypeError, ValueError):
            return jsonify({"error": "staff_id không hợp lệ"}), 400

        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                if get_user_role(cursor, staff_id) != "staff":
                    return jsonify({"error": "Tài khoản không có quyền tư vấn viên"}), 403
                return view(cursor, connection, staff_id, *args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("[Staff] Lỗi xử lý %s", view.__name__)
            return jsonify({"error": "Lỗi hệ thống, vui lòng thử lại sau."}), 500
        finally:
            connection.close()
    return wrapper


def _not_found():
    return jsonify({"error": "Không tìm thấy phiên chat"}), 404


# ---- phiên chat ------------------------------------------------------------------------------
@staff_bp.route('/api/staff/conversations', methods=['GET'])
@staff_endpoint
def staff_list_conversations(cursor, _connection, staff_id):
    scope = request.args.get("scope", "waiting")
    if scope not in STAFF_SCOPES:
        return jsonify({"error": f"scope phải là một trong: {', '.join(STAFF_SCOPES)}"}), 400
    conversations = list_waiting_conversations(cursor, scope=scope, staff_id=staff_id)
    return jsonify({"status": "success", "staff_id": staff_id, "scope": scope,
                    "conversations": conversations})


@staff_bp.route('/api/staff/conversations/<int:conversation_id>/messages', methods=['GET'])
@staff_endpoint
def staff_get_messages(cursor, _connection, staff_id, conversation_id):
    conversation = get_conversation(cursor, conversation_id)
    if not conversation:
        return _not_found()
    messages = get_conversation_messages(cursor, conversation_id)
    return jsonify({"status": "success", "staff_id": staff_id,
                    "conversation": conversation, "messages": messages})


@staff_bp.route('/api/staff/conversations/<int:conversation_id>/claim', methods=['POST'])
@staff_endpoint
def staff_claim(cursor, connection, staff_id, conversation_id):
    conversation = get_conversation(cursor, conversation_id)
    if not conversation:
        return _not_found()
    already_mine = (conversation["status"] == "agent"
                    and conversation["assigned_staff_id"] == staff_id)
    if not already_mine:
        if conversation["status"] != "waiting_agent":
            # Chặn 2 nhân viên cùng nhận 1 phiên, hoặc nhận phiên bot/đã đóng.
            return jsonify({"error": "Phiên chat không ở trạng thái chờ tư vấn viên",
                            "conversation_status": conversation["status"]}), 409
        assign_staff(cursor, connection, conversation_id, staff_id)
        save_message(cursor, connection, conversation_id, 'system', CLAIM_MESSAGE)
    return jsonify({"status": "success", "conversation_id": conversation_id,
                    "staff_id": staff_id, "conversation_status": "agent"})


@staff_bp.route('/api/staff/conversations/<int:conversation_id>/reply', methods=['POST'])
@staff_endpoint
def staff_reply(cursor, connection, staff_id, conversation_id):
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Vui lòng nhập nội dung trả lời"}), 400

    conversation = get_conversation(cursor, conversation_id)
    if not conversation:
        return _not_found()
    if conversation["status"] not in ("agent", "waiting_agent"):
        return jsonify({"error": "Phiên chat không ở trạng thái tư vấn viên"}), 409
    save_message(cursor, connection, conversation_id, 'staff', message)
    delivered = _deliver(conversation_id, message)
    return jsonify({"status": "success", "conversation_id": conversation_id,
                    "staff_id": staff_id, "channel_delivered": delivered})


@staff_bp.route('/api/staff/conversations/<int:conversation_id>/close', methods=['POST'])
@staff_endpoint
def staff_close(cursor, connection, staff_id, conversation_id):
    conversation = get_conversation(cursor, conversation_id)
    if not conversation:
        return _not_found()
    close_conversation(cursor, connection, conversation_id)
    save_message(cursor, connection, conversation_id, 'system', CLOSE_MESSAGE)
    delivered = _deliver(conversation_id, CLOSE_MESSAGE)
    return jsonify({"status": "success", "conversation_id": conversation_id,
                    "staff_id": staff_id, "channel_delivered": delivered})


# ---- thông báo ------------------------------------------------------------------------------
@staff_bp.route('/api/staff/notifications', methods=['GET'])
@staff_endpoint
def staff_notifications(cursor, _connection, staff_id):
    notifications = list_unread_agent_notifications(cursor)
    return jsonify({"status": "success", "staff_id": staff_id, "notifications": notifications})


@staff_bp.route('/api/staff/notifications/<int:notification_id>/read', methods=['POST'])
@staff_endpoint
def staff_notification_read(cursor, connection, staff_id, notification_id):
    mark_agent_notification_read(cursor, connection, notification_id)
    return jsonify({"status": "success", "staff_id": staff_id, "notification_id": notification_id})


# ---- ticket ngoài giờ -----------------------------------------------------------------------
@staff_bp.route('/api/staff/tickets', methods=['GET'])
@staff_endpoint
def staff_list_tickets(cursor, _connection, staff_id):
    status = request.args.get("status") or None
    if status and status not in TICKET_STATUSES:
        return jsonify({"error": f"status phải là một trong: {', '.join(TICKET_STATUSES)}"}), 400
    return jsonify({"status": "success", "staff_id": staff_id,
                    "tickets": list_support_tickets(cursor, status)})


@staff_bp.route('/api/staff/tickets/<int:ticket_id>/status', methods=['POST'])
@staff_endpoint
def staff_update_ticket(cursor, connection, staff_id, ticket_id):
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in TICKET_STATUSES:
        return jsonify({"error": f"status phải là một trong: {', '.join(TICKET_STATUSES)}"}), 400
    if not get_support_ticket(cursor, ticket_id):
        return jsonify({"error": "Không tìm thấy ticket"}), 404
    update_support_ticket_status(cursor, connection, ticket_id, status, staff_id)
    return jsonify({"status": "success", "ticket_id": ticket_id, "ticket_status": status})
