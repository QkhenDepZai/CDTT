import logging

from flask import Blueprint, request, jsonify

from channels.dispatcher import deliver_staff_reply

from database import (
    get_db_connection,
    get_conversation,
    get_conversation_messages,
    list_waiting_conversations,
    assign_staff,
    save_message,
    close_conversation,
    get_user_role,
    list_unread_agent_notifications,
    mark_agent_notification_read,
)

staff_bp = Blueprint('staff', __name__)
logger = logging.getLogger("pmaster.routes.staff")

CLOSE_MESSAGE = "Tư vấn viên đã đánh dấu hoàn thành, phiên chat đã đóng."


def _deliver(conversation_id, text):
    """Đẩy tin ra Messenger/Zalo nếu phiên thuộc các kênh đó. None = phiên web
    (widget tự lấy tin qua API); False = gửi thất bại (đã log)."""
    try:
        return deliver_staff_reply(conversation_id, text)
    except Exception:  # noqa: BLE001 - tin đã lưu DB, lỗi gửi kênh không làm hỏng request
        logger.exception("[Staff] Không đẩy được tin ra kênh cho phiên %s", conversation_id)
        return False


def _staff_id_from_request(data=None):
    data = data or request.get_json(silent=True) or {}
    return data.get("staff_id") or request.headers.get("X-Staff-Id")


def _require_staff(staff_id):
    if not staff_id:
        return None, (jsonify({"error": "Thiếu staff_id"}), 400)
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            role = get_user_role(cursor, staff_id)
        if role != "staff":
            return None, (jsonify({"error": "Tài khoản không có quyền tư vấn viên"}), 403)
    finally:
        connection.close()
    return int(staff_id), None


@staff_bp.route('/api/staff/conversations', methods=['GET'])
def staff_list_waiting():
    staff_id, error = _require_staff(request.args.get("staff_id") or request.headers.get("X-Staff-Id"))
    if error:
        return error
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversations = list_waiting_conversations(cursor)
        return jsonify({"status": "success", "staff_id": staff_id, "conversations": conversations})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


@staff_bp.route('/api/staff/notifications', methods=['GET'])
def staff_notifications():
    staff_id, error = _require_staff(request.args.get("staff_id") or request.headers.get("X-Staff-Id"))
    if error:
        return error
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            notifications = list_unread_agent_notifications(cursor)
        return jsonify({"status": "success", "staff_id": staff_id, "notifications": notifications})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


@staff_bp.route('/api/staff/notifications/<int:notification_id>/read', methods=['POST'])
def staff_notification_read(notification_id):
    data = request.get_json(silent=True) or {}
    staff_id, error = _require_staff(_staff_id_from_request(data))
    if error:
        return error
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            mark_agent_notification_read(cursor, connection, notification_id)
        return jsonify({"status": "success", "staff_id": staff_id, "notification_id": notification_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


@staff_bp.route('/api/staff/conversations/<int:conversation_id>/messages', methods=['GET'])
def staff_get_messages(conversation_id):
    staff_id, error = _require_staff(request.args.get("staff_id") or request.headers.get("X-Staff-Id"))
    if error:
        return error
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversation = get_conversation(cursor, conversation_id)
            if not conversation:
                return jsonify({"error": "Không tìm thấy phiên chat"}), 404
            messages = get_conversation_messages(cursor, conversation_id)
        return jsonify({"status": "success", "staff_id": staff_id, "conversation": conversation, "messages": messages})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


@staff_bp.route('/api/staff/conversations/<int:conversation_id>/claim', methods=['POST'])
def staff_claim(conversation_id):
    data = request.get_json(silent=True) or {}
    staff_id, error = _require_staff(_staff_id_from_request(data))
    if error:
        return error
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversation = get_conversation(cursor, conversation_id)
            if not conversation:
                return jsonify({"error": "Không tìm thấy phiên chat"}), 404
            assign_staff(cursor, connection, conversation_id, staff_id)
            save_message(cursor, connection, conversation_id, 'system', "Tư vấn viên đã tiếp nhận phiên chat.")
        return jsonify({"status": "success", "conversation_id": conversation_id, "staff_id": staff_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


@staff_bp.route('/api/staff/conversations/<int:conversation_id>/reply', methods=['POST'])
def staff_reply(conversation_id):
    data = request.get_json(silent=True) or {}
    staff_id, error = _require_staff(_staff_id_from_request(data))
    if error:
        return error
    message = data.get("message")
    if not message:
        return jsonify({"error": "Vui lòng nhập nội dung trả lời"}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversation = get_conversation(cursor, conversation_id)
            if not conversation:
                return jsonify({"error": "Không tìm thấy phiên chat"}), 404
            if conversation["status"] not in ("agent", "waiting_agent"):
                return jsonify({"error": "Phiên chat không ở trạng thái tư vấn viên"}), 409
            save_message(cursor, connection, conversation_id, 'staff', message)
        delivered = _deliver(conversation_id, message)
        return jsonify({"status": "success", "conversation_id": conversation_id,
                        "staff_id": staff_id, "channel_delivered": delivered})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


@staff_bp.route('/api/staff/conversations/<int:conversation_id>/close', methods=['POST'])
def staff_close(conversation_id):
    data = request.get_json(silent=True) or {}
    staff_id, error = _require_staff(_staff_id_from_request(data))
    if error:
        return error
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversation = get_conversation(cursor, conversation_id)
            if not conversation:
                return jsonify({"error": "Không tìm thấy phiên chat"}), 404
            close_conversation(cursor, connection, conversation_id)
            save_message(cursor, connection, conversation_id, 'system', CLOSE_MESSAGE)
        delivered = _deliver(conversation_id, CLOSE_MESSAGE)
        return jsonify({"status": "success", "conversation_id": conversation_id,
                        "staff_id": staff_id, "channel_delivered": delivered})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()
