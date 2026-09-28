import json

from flask import Blueprint, request, jsonify

from database import (
    get_db_connection,
    get_or_create_user,
    get_or_create_conversation,
    save_message,
    get_conversation_messages,
    get_conversation,
    set_conversation_status,
    increment_fail_count,
    reset_fail_count,
    log_violation,
    get_faq_suggestions,
    get_faq_by_id,
    create_agent_notification,
    list_user_conversations,
)
from faq_matcher import find_best_faq_match
from moderation import check_violation
from image_handler import validate_image, save_image, build_image_url, prepare_image_for_gemini
from security import (
    get_request_user_token,
    issue_user_token,
    verify_user_token,
    detect_prompt_injection,
    PROMPT_INJECTION_MESSAGE,
    detect_internal_data_request,
    INTERNAL_DATA_REQUEST_MESSAGE,
)
from conversation_memory import build_history_for_gemini
from rag_service import ImageInput, get_rag_service
from config import ENFORCE_USER_TOKEN

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

GREETING_TEXT = (
    "Chào bạn! Mình là trợ lý ảo của Python Master 2026. "
    "Bạn có thể chọn nhanh 1 câu hỏi bên dưới, hoặc gõ câu hỏi của bạn."
)


def _save_rag_reply(cursor, connection, conversation_id, answer):
    """Lưu câu trả lời AI kèm dữ liệu truy vết RAG (chunk đã dùng, độ trễ)."""
    save_message(
        cursor, connection, conversation_id, 'model', answer.reply,
        answer_status=answer.status,
        retrieved_chunk_ids=answer.chunk_ids,
        latency_ms=answer.latency_ms,
    )


def _apply_ai_result(cursor, connection, conversation_id, model_reply, kind, response_payload):
    """Xử lý fail_count / escalate theo kind trả về từ Gemini."""
    if kind == "answered":
        reset_fail_count(cursor, connection, conversation_id)
    elif kind == "out_of_scope":
        pass
    elif kind == "cannot_answer":
        fail_count = increment_fail_count(cursor, connection, conversation_id)
        response_payload["fail_count"] = fail_count
        if fail_count >= 3:
            escalate_msg = (
                "Hệ thống chưa thể trả lời câu hỏi này sau nhiều lần thử. "
                "Đã chuyển yêu cầu của bạn tới tư vấn viên, vui lòng chờ trong giây lát."
            )
            save_message(cursor, connection, conversation_id, 'system', escalate_msg)
            create_agent_notification(
                cursor, connection, conversation_id,
                "AI không trả lời được 3 lần liên tiếp"
            )
            response_payload["reply"] = f"{model_reply}\n\n{escalate_msg}"
            response_payload["escalated"] = True
    # overloaded -> lỗi hạ tầng tạm thời, không tính vào fail_count


# ============================================================
# 1. KHỞI TẠO SESSION
# ============================================================
@chat_bp.route('/api/chat/init', methods=['POST'])
def chat_init():
    data = request.json or {}
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

        return jsonify({
            "status": "success",
            "user_id": user_id,
            "conversation_id": conversation_id,
            "greeting": GREETING_TEXT,
            "faq_suggestions": faqs,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
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
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


# ============================================================
# 2a. CHỌN FAQ CÓ SẴN -> trả lời trực tiếp, không qua fuzzy-match
# ============================================================
@chat_bp.route('/api/chat/faq/<int:faq_id>', methods=['POST'])
def chat_faq_direct(faq_id):
    data = request.json or {}
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
            conversation_id = conversation["id"]

            save_message(cursor, connection, conversation_id, 'user',
                         f"[Chọn FAQ] {faq['intent']}")
            save_message(cursor, connection, conversation_id, 'model', faq["tra_loi_chuan"])
            reset_fail_count(cursor, connection, conversation_id)

        return jsonify({
            "status": "success",
            "user_id": user_id,
            "conversation_id": conversation_id,
            "reply": faq["tra_loi_chuan"],
            "used_faq": True,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


# ============================================================
# 2b. NGƯỜI DÙNG BẤM "GẶP TƯ VẤN VIÊN"
# ============================================================
@chat_bp.route('/api/chat/request-agent', methods=['POST'])
def chat_request_agent():
    data = request.json or {}
    conversation_id = data.get("conversation_id")

    if not conversation_id:
        return jsonify({"error": "Thiếu conversation_id"}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            conversation = get_conversation(cursor, conversation_id)
            if not conversation:
                return jsonify({"error": "Không tìm thấy phiên chat"}), 404

            set_conversation_status(cursor, connection, conversation_id, 'waiting_agent')
            save_message(
                cursor, connection, conversation_id, 'system',
                "Thí sinh yêu cầu gặp tư vấn viên."
            )
            create_agent_notification(
                cursor, connection, conversation_id,
                "Người dùng chủ động yêu cầu gặp tư vấn viên"
            )

        return jsonify({
            "status": "success",
            "conversation_id": conversation_id,
            "message": "Đã chuyển yêu cầu tới tư vấn viên, vui lòng chờ trong giây lát.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()


# ============================================================
# 2c. TIN NHẮN TỰ DO (text) - luồng chính
# ============================================================
@chat_bp.route('/api/chat', methods=['POST'])
def chat_endpoint():
    data = request.json or {}
    user_message = data.get("message")
    conversation_id = data.get("conversation_id")
    user_id = data.get("user_id")
    site_id = getattr(request, 'site_id', None)

    if not user_message:
        return jsonify({"error": "Vui lòng nhập tin nhắn"}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # 1. Xử lý User
            user_id = get_or_create_user(cursor, connection, user_id)

            # 2. Xử lý Conversation
            conversation = get_or_create_conversation(
                cursor, connection, conversation_id, user_id,
                title=user_message[:30], site_id=site_id,
            )
            conversation_id = conversation["id"]
            status = conversation["status"]

            # 2.1 Nếu phiên đã đóng -> coi như thí sinh quay lại, mở lại làm việc với bot
            if status == 'closed':
                set_conversation_status(cursor, connection, conversation_id, 'bot')
                reset_fail_count(cursor, connection, conversation_id)
                status = 'bot'

            # 2.2 Nếu đang chờ/đang được tư vấn viên xử lý -> KHÔNG cho bot trả lời nữa,
            #     chỉ lưu tin nhắn để nhân viên xem lại lịch sử
            if status in ('waiting_agent', 'agent'):
                save_message(cursor, connection, conversation_id, 'user', user_message)
                pending_msg = (
                    "Yêu cầu của bạn đang chờ tư vấn viên."
                    if status == 'waiting_agent'
                    else "Tư vấn viên đang xử lý yêu cầu của bạn, vui lòng chờ phản hồi."
                )
                return jsonify({
                    "status": "success",
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "reply": pending_msg,
                    "handled_by": "agent",
                })

            # 3. Chặn Prompt Injection + nội dung vi phạm TRƯỚC khi xử lý nghiệp vụ
            is_injection, injection_reason = detect_prompt_injection(user_message)
            if is_injection:
                save_message(cursor, connection, conversation_id, 'user', user_message)
                log_violation(cursor, connection, conversation_id, user_id, user_message,
                              f"PROMPT_INJECTION:{injection_reason}")
                save_message(cursor, connection, conversation_id, 'system', PROMPT_INJECTION_MESSAGE)
                return jsonify({
                    "status": "success",
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "reply": PROMPT_INJECTION_MESSAGE,
                    "blocked": True,
                    "reason": "prompt_injection",
                })

            # 3b. Chặn yêu cầu xin số liệu/danh sách nội bộ (người đăng ký, thí sinh...)
            # TRƯỚC khi vào FAQ matcher - không để việc trả lời/từ chối phụ thuộc vào
            # FAQ có match "trúng may rủi" hay không (xem giải thích trong security.py).
            is_internal_request, internal_reason = detect_internal_data_request(user_message)
            if is_internal_request:
                save_message(cursor, connection, conversation_id, 'user', user_message)
                log_violation(cursor, connection, conversation_id, user_id, user_message,
                              f"INTERNAL_DATA_REQUEST:{internal_reason}")
                save_message(cursor, connection, conversation_id, 'system', INTERNAL_DATA_REQUEST_MESSAGE)
                return jsonify({
                    "status": "success",
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "reply": INTERNAL_DATA_REQUEST_MESSAGE,
                    "blocked": True,
                    "reason": "internal_data_request",
                })

            is_violation, reason = check_violation(user_message)
            if is_violation:
                save_message(cursor, connection, conversation_id, 'user', user_message)
                log_violation(cursor, connection, conversation_id, user_id, user_message, reason)
                refuse_msg = "Xin lỗi, nội dung của bạn không phù hợp và đã bị từ chối."
                save_message(cursor, connection, conversation_id, 'system', refuse_msg)
                return jsonify({
                    "status": "success",
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "reply": refuse_msg,
                    "violation": True,
                })

            # 4. Lưu tin nhắn của User (hợp lệ)
            save_message(cursor, connection, conversation_id, 'user', user_message)

            # 5. Tra cứu FAQ (fuzzy-match) trước
            faq_row = find_best_faq_match(cursor, user_message)

            if faq_row:
                model_reply = faq_row['tra_loi_chuan']
                reset_fail_count(cursor, connection, conversation_id)
                save_message(cursor, connection, conversation_id, 'model', model_reply,
                             answer_status='faq')
                return jsonify({
                    "status": "success",
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "reply": model_reply,
                    "used_faq": True,
                })

            # 6. Không có FAQ khớp trực tiếp -> RAG: truy vấn Knowledge Base
            #    (tài liệu + FAQ đã embed) rồi ghép ngữ cảnh cho Gemini trả lời.
            db_messages = get_conversation_messages(cursor, conversation_id)
            history = build_history_for_gemini(cursor, connection, conversation_id, db_messages)
            answer = get_rag_service().answer(user_message, history, cursor=cursor)
            _save_rag_reply(cursor, connection, conversation_id, answer)

            response_payload = {
                "status": "success",
                "user_id": user_id,
                "conversation_id": conversation_id,
                "reply": answer.reply,
                "used_faq": False,
                "answer_status": answer.status,
                "sources": answer.sources_payload(),
                "latency_ms": answer.latency_ms,
            }
            _apply_ai_result(cursor, connection, conversation_id, answer.reply, answer.status,
                             response_payload)

            return jsonify(response_payload)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
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
            # 1-2. User + Conversation
            user_id = get_or_create_user(cursor, connection, user_id)
            conversation = get_or_create_conversation(
                cursor, connection, conversation_id, user_id,
                title=(user_message[:30] if user_message else "Gửi hình ảnh"),
                site_id=site_id,
            )
            conversation_id = conversation["id"]
            status = conversation["status"]

            if status == 'closed':
                set_conversation_status(cursor, connection, conversation_id, 'bot')
                reset_fail_count(cursor, connection, conversation_id)
                status = 'bot'

            # Lưu file vật lý + build URL công khai TRƯỚC, để dù nhánh nào cũng lưu được ảnh
            relative_path, _ = save_image(image_data["bytes"], image_data["ext"])
            image_url = build_image_url(relative_path)

            # 2.2 Đang chờ/đang được tư vấn viên xử lý -> chỉ lưu lại, không gọi AI
            if status in ('waiting_agent', 'agent'):
                save_message(cursor, connection, conversation_id, 'user', user_message,
                             image_url=image_url)
                pending_msg = (
                    "Yêu cầu của bạn đang chờ tư vấn viên."
                    if status == 'waiting_agent'
                    else "Tư vấn viên đang xử lý yêu cầu của bạn, vui lòng chờ phản hồi."
                )
                return jsonify({
                    "status": "success",
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "reply": pending_msg,
                    "image_url": image_url,
                    "handled_by": "agent",
                })

            if user_message:
                is_injection, injection_reason = detect_prompt_injection(user_message)
                if is_injection:
                    save_message(cursor, connection, conversation_id, 'user', user_message,
                                 image_url=image_url)
                    log_violation(cursor, connection, conversation_id, user_id, user_message,
                                  f"PROMPT_INJECTION:{injection_reason}")
                    save_message(cursor, connection, conversation_id, 'system', PROMPT_INJECTION_MESSAGE)
                    return jsonify({
                        "status": "success",
                        "user_id": user_id,
                        "conversation_id": conversation_id,
                        "reply": PROMPT_INJECTION_MESSAGE,
                        "image_url": image_url,
                        "blocked": True,
                        "reason": "prompt_injection",
                    })

                is_internal_request, internal_reason = detect_internal_data_request(user_message)
                if is_internal_request:
                    save_message(cursor, connection, conversation_id, 'user', user_message,
                                 image_url=image_url)
                    log_violation(cursor, connection, conversation_id, user_id, user_message,
                                  f"INTERNAL_DATA_REQUEST:{internal_reason}")
                    save_message(cursor, connection, conversation_id, 'system', INTERNAL_DATA_REQUEST_MESSAGE)
                    return jsonify({
                        "status": "success",
                        "user_id": user_id,
                        "conversation_id": conversation_id,
                        "reply": INTERNAL_DATA_REQUEST_MESSAGE,
                        "image_url": image_url,
                        "blocked": True,
                        "reason": "internal_data_request",
                    })

                is_violation, reason = check_violation(user_message)
                if is_violation:
                    save_message(cursor, connection, conversation_id, 'user', user_message,
                                 image_url=image_url)
                    log_violation(cursor, connection, conversation_id, user_id, user_message, reason)
                    refuse_msg = "Xin lỗi, nội dung của bạn không phù hợp và đã bị từ chối."
                    save_message(cursor, connection, conversation_id, 'system', refuse_msg)
                    return jsonify({
                        "status": "success",
                        "user_id": user_id,
                        "conversation_id": conversation_id,
                        "reply": refuse_msg,
                        "image_url": image_url,
                        "violation": True,
                    })

            # 5. Lưu tin nhắn User (ảnh + text nếu có)
            save_message(cursor, connection, conversation_id, 'user', user_message,
                         image_url=image_url)

            # Ảnh gửi tới Gemini để phân tích; FAQ matcher chỉ áp dụng cho text.
            # Nếu có kèm câu hỏi, rag_service vẫn truy vấn KB theo phần text.
            db_messages = get_conversation_messages(cursor, conversation_id)
            history = build_history_for_gemini(cursor, connection, conversation_id, db_messages)
            # Resize ảnh trước khi gửi Gemini để giảm token/latency (mục 9 yêu cầu):
            # ảnh chụp màn hình/điện thoại có thể vượt xa độ phân giải model cần.
            resized_bytes, resized_mime = prepare_image_for_gemini(
                image_data["bytes"], image_data["mime_type"]
            )
            answer = get_rag_service().answer(
                user_message, history, image=ImageInput(resized_bytes, resized_mime), cursor=cursor,
            )
            _save_rag_reply(cursor, connection, conversation_id, answer)

            response_payload = {
                "status": "success",
                "user_id": user_id,
                "conversation_id": conversation_id,
                "reply": answer.reply,
                "image_url": image_url,
                "used_faq": False,
                "answer_status": answer.status,
                "sources": answer.sources_payload(),
                "latency_ms": answer.latency_ms,
            }
            _apply_ai_result(cursor, connection, conversation_id, answer.reply, answer.status,
                             response_payload)

            return jsonify(response_payload)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()
