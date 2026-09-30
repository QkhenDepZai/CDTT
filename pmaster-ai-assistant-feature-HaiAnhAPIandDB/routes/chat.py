import json

from flask import Blueprint, request, jsonify

import logging

from database import (
    get_db_connection,
    get_or_create_user,
    get_or_create_conversation,
    save_message,
    get_conversation,
    get_faq_suggestions,
    get_faq_by_id,
    get_last_message_id,
    get_messages_after,
    get_user_conversation,
    list_user_conversations,
)
from image_handler import validate_image, save_image, build_image_url, prepare_image_for_gemini
from security import get_request_user_token, issue_user_token, verify_user_token
from rag_service import ImageInput
from config import ENFORCE_USER_TOKEN
import chat_service
import handover
from chat_service import GREETING_TEXT

chat_bp = Blueprint('chat', __name__)


def _request_user_id():
    if request.is_json:
        return (request.get_json(silent=True) or {}).get("user_id")
    return request.form.get("user_id")


@chat_bp.before_request
def _enforce_user_token():
    """ENFORCE_USER_TOKEN=true: ai gửi user_id có sẵn phải kèm đúng user_token,
    tránh ghi tin nhắn/đọc phản hồi trong phiên của người khác."""
    if not ENFORCE_USER_TOKEN or request.method == "OPTIONS":
        return None
    user_id = _request_user_id()
    if user_id in (None, ""):
        return None  # người dùng mới -> hệ thống tự tạo user và cấp token
    try:
        valid = verify_user_token(int(user_id), get_request_user_token())
    except (TypeError, ValueError):
        valid = False
    if not valid:
        return jsonify({"error": "user_token không hợp lệ cho user_id này."}), 401
    return None


@chat_bp.after_request
def _attach_user_token(response):
    """Gắn user_token vào mọi response thành công có user_id, để client lưu lại
    và dùng cho GET /api/history/<user_id> (và các lần chat sau)."""
    if response.status_code != 200 or not response.is_json:
        return response
    body = response.get_json(silent=True)
    if isinstance(body, dict) and body.get("user_id") and "user_token" not in body:
        token = issue_user_token(body["user_id"])
        if token:
            body["user_token"] = token
            response.set_data(json.dumps(body, ensure_ascii=False, default=str))
    return response


logger = logging.getLogger("pmaster.routes.chat")

SERVER_ERROR = ({"error": "Lỗi hệ thống, vui lòng thử lại sau."}, 500)


def _support_info():
    return {"hours": handover.support_hours_label(),
            "available": handover.is_within_support_hours()}


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _server_error(context):
    # Ghi chi tiết vào log, KHÔNG trả str(exception) ra ngoài (có thể lộ SQL/cấu hình).
    logger.exception("[Chat] Lỗi xử lý %s", context)
    return jsonify(SERVER_ERROR[0]), SERVER_ERROR[1]


# ============================================================
# 1. KHỞI TẠO SESSION
# ============================================================
@chat_bp.route('/api/chat/init', methods=['POST'])
def chat_init():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    site_id = getattr(request, 'site_id', None)

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            user_id = get_or_create_user(cursor, connection, user_id)
            conversation_id = get_or_create_conversation(
                cursor, connection,
                conversation_id=None,
                user_id=user_id,
                title="Đoạn chat mới",
                site_id=site_id,
            )["id"]

            save_message(cursor, connection, conversation_id, 'system', GREETING_TEXT)
            faqs = get_faq_suggestions(cursor, limit=5)
            last_message_id = get_last_message_id(cursor, conversation_id)

        return jsonify({
            "status": "success",
            "user_id": user_id,
            "conversation_id": conversation_id,
            "greeting": GREETING_TEXT,
            "faq_suggestions": faqs,
            "support": _support_info(),
            "last_message_id": last_message_id,
        })
    except Exception:  # noqa: BLE001
        return _server_error("chat_init")
    finally:
        connection.close()


# ============================================================
# 1b. LỊCH SỬ CÁC PHIÊN CHAT CỦA NGƯỜI DÙNG
# ============================================================
@chat_bp.route('/api/chat/history/<int:user_id>', methods=['GET'])
def chat_history(user_id):
    # Trước đây endpoint này trả danh sách phiên chat cho BẤT KỲ ai biết
    # user_id (dò số tăng dần là xem được của người khác - vi phạm D1-13).
    if not verify_user_token(user_id, get_request_user_token()):
        return jsonify({"error": "Không có quyền xem lịch sử này (thiếu hoặc sai user_token)."}), 403
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversations = list_user_conversations(cursor, user_id)
        return jsonify({
            "status": "success",
            "user_id": user_id,
            "conversations": conversations,
        })
    except Exception:  # noqa: BLE001
        return _server_error("chat_history")
    finally:
        connection.close()


# ============================================================
# 2a. CHỌN FAQ CÓ SẴN -> trả lời trực tiếp, không qua fuzzy-match
# ============================================================
@chat_bp.route('/api/chat/faq/<int:faq_id>', methods=['POST'])
def chat_faq_direct(faq_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    conversation_id = data.get("conversation_id")
    site_id = getattr(request, 'site_id', None)

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            faq = get_faq_by_id(cursor, faq_id)
            if not faq:
                return jsonify({"error": "Không tìm thấy FAQ"}), 404

            user_id = get_or_create_user(cursor, connection, user_id)
            conversation = get_or_create_conversation(
                cursor, connection, conversation_id, user_id,
                title=faq["intent"] or "Đoạn chat mới", site_id=site_id,
            )
            if conversation["status"] in ("waiting_agent", "agent"):
                # Đang gặp tư vấn viên: bot không tự trả lời (TC-HANDOVER-13).
                payload = chat_service.process_message(
                    cursor, connection, conversation=conversation, user_id=user_id,
                    text=f"[Chọn FAQ] {faq['intent']}",
                )
            else:
                reply = chat_service.answer_faq(cursor, connection, conversation["id"], faq)
                payload = {"reply": reply, "used_faq": True, "answer_status": "faq"}
            payload["last_message_id"] = get_last_message_id(cursor, conversation["id"])

        return jsonify({
            "status": "success",
            "user_id": user_id,
            "conversation_id": conversation["id"],
            **payload,
        })
    except Exception:  # noqa: BLE001
        return _server_error("chat_faq_direct")
    finally:
        connection.close()


# ============================================================
# 2b. NGƯỜI DÙNG BẤM "GẶP TƯ VẤN VIÊN"
# ============================================================
@chat_bp.route('/api/chat/request-agent', methods=['POST'])
def chat_request_agent():
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    user_id = data.get("user_id")

    if not conversation_id:
        return jsonify({"error": "Thiếu conversation_id"}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversation = get_conversation(cursor, conversation_id)
            if not conversation or (user_id and str(conversation["user_id"]) != str(user_id)):
                return jsonify({"error": "Không tìm thấy phiên chat"}), 404

            if conversation["status"] in ("waiting_agent", "agent"):
                # Đã ở hàng chờ/đang được hỗ trợ: không tạo thêm thông báo trùng.
                message = (chat_service.WAITING_AGENT_MESSAGE
                           if conversation["status"] == "waiting_agent"
                           else chat_service.AGENT_HANDLING_MESSAGE)
                mode, status = handover.LIVE, conversation["status"]
            else:
                result = chat_service.request_agent(cursor, connection, conversation["id"])
                message, mode = result.message, result.mode
                status = "bot" if result.after_hours else "waiting_agent"
            last_message_id = get_last_message_id(cursor, conversation["id"])

        return jsonify({
            "status": "success",
            "conversation_id": conversation["id"],
            "message": message,
            "handover_mode": mode,
            "after_hours": mode == handover.AFTER_HOURS,
            "conversation_status": status,
            "support": _support_info(),
            "last_message_id": last_message_id,
        })
    except Exception:  # noqa: BLE001
        return _server_error("chat_request_agent")
    finally:
        connection.close()


# ============================================================
# 2b'. ĐỂ LẠI THÔNG TIN HỖ TRỢ (ngoài giờ trực) - TC-HANDOVER-08/09
# ============================================================
@chat_bp.route('/api/chat/tickets', methods=['POST'])
def chat_create_ticket():
    data = request.get_json(silent=True) or {}
    conversation_id = _positive_int(data.get("conversation_id"))
    user_id = _positive_int(data.get("user_id"))
    if not conversation_id or not user_id:
        return jsonify({"error": "Thiếu user_id hoặc conversation_id"}), 400

    ticket, error = handover.validate_ticket(data)
    if error:
        return jsonify({"error": error}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            if not get_user_conversation(cursor, user_id, conversation_id):
                return jsonify({"error": "Không tìm thấy phiên chat"}), 404
            ticket_id = handover.open_ticket(cursor, connection, conversation_id, user_id, ticket)
            last_message_id = get_last_message_id(cursor, conversation_id)
        return jsonify({
            "status": "success",
            "ticket_id": ticket_id,
            "last_message_id": last_message_id,
            "message": ("Đã ghi nhận yêu cầu hỗ trợ của bạn. Tư vấn viên sẽ liên hệ lại "
                        "trong giờ làm việc."),
        }), 201
    except Exception:  # noqa: BLE001
        return _server_error("chat_create_ticket")
    finally:
        connection.close()


# ============================================================
# 2b''. TIN NHẮN CỦA 1 PHIÊN - widget khôi phục phiên & nhận tin tư vấn viên
# ============================================================
@chat_bp.route('/api/chat/conversations/<int:conversation_id>/messages', methods=['GET'])
def chat_conversation_messages(conversation_id):
    """GET ?user_id=..&after_id=.. (header X-User-Token). Luôn bắt buộc
    user_token: đây là API ĐỌC dữ liệu hội thoại (D1-13)."""
    user_id = _positive_int(request.args.get("user_id"))
    after_id = _positive_int(request.args.get("after_id")) or 0
    if not user_id or not verify_user_token(user_id, get_request_user_token()):
        return jsonify({"error": "Không có quyền xem phiên chat này (thiếu hoặc sai user_token)."}), 403

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversation = get_user_conversation(cursor, user_id, conversation_id)
            if not conversation:
                return jsonify({"error": "Không tìm thấy phiên chat"}), 404
            messages = get_messages_after(cursor, conversation_id, after_id)
        return jsonify({
            "status": "success",
            "conversation": {"id": conversation["id"], "title": conversation["title"],
                             "status": conversation["status"]},
            "messages": [
                {**m, "created_at": m["created_at"].isoformat(sep=" ") if m["created_at"] else None}
                for m in messages
            ],
        })
    except Exception:  # noqa: BLE001
        return _server_error("chat_conversation_messages")
    finally:
        connection.close()


# ============================================================
# 2c. TIN NHẮN TỰ DO (text) - luồng chính, logic ở chat_service.py
# ============================================================
@chat_bp.route('/api/chat', methods=['POST'])
def chat_endpoint():
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    conversation_id = data.get("conversation_id")
    user_id = data.get("user_id")
    site_id = getattr(request, 'site_id', None)

    if not user_message:
        return jsonify({"error": "Vui lòng nhập tin nhắn"}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            user_id = get_or_create_user(cursor, connection, user_id)
            conversation = get_or_create_conversation(
                cursor, connection, conversation_id, user_id,
                title=user_message[:30], site_id=site_id,
            )
            payload = chat_service.process_message(
                cursor, connection, conversation=conversation, user_id=user_id,
                text=user_message,
            )
            payload["last_message_id"] = get_last_message_id(cursor, conversation["id"])
        return jsonify({
            "status": "success",
            "user_id": user_id,
            "conversation_id": conversation["id"],
            **payload,
        })
    except Exception:  # noqa: BLE001
        return _server_error("chat_endpoint")
    finally:
        connection.close()


# ============================================================
# 2d. GỬI ẢNH (+ text tuỳ chọn) - Module Multimodal
# ============================================================
@chat_bp.route('/api/chat/image', methods=['POST'])
def chat_image_endpoint():
    site_id = getattr(request, 'site_id', None)
    user_id = request.form.get("user_id")
    conversation_id = request.form.get("conversation_id")
    user_message = (request.form.get("message") or "").strip()

    image_file = request.files.get("image")
    if not image_file:
        return jsonify({"error": "Vui lòng đính kèm ảnh"}), 400

    is_valid, error_msg, image_data = validate_image(image_file)
    if not is_valid:
        return jsonify({"error": error_msg}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            user_id = get_or_create_user(cursor, connection, user_id)
            conversation = get_or_create_conversation(
                cursor, connection, conversation_id, user_id,
                title=(user_message[:30] if user_message else "Gửi hình ảnh"),
                site_id=site_id,
            )

            # Lưu file gốc + URL công khai TRƯỚC, để nhánh nào cũng lưu được ảnh
            relative_path, _ = save_image(image_data["bytes"], image_data["ext"])
            image_url = build_image_url(relative_path)

            # Resize ảnh trước khi gửi Gemini để giảm token/latency.
            resized_bytes, resized_mime = prepare_image_for_gemini(
                image_data["bytes"], image_data["mime_type"]
            )
            payload = chat_service.process_message(
                cursor, connection, conversation=conversation, user_id=user_id,
                text=user_message, image=ImageInput(resized_bytes, resized_mime),
                image_url=image_url,
            )
            payload["last_message_id"] = get_last_message_id(cursor, conversation["id"])
        return jsonify({
            "status": "success",
            "user_id": user_id,
            "conversation_id": conversation["id"],
            "image_url": image_url,
            **payload,
        })
    except Exception:  # noqa: BLE001
        return _server_error("chat_image_endpoint")
    finally:
        connection.close()
