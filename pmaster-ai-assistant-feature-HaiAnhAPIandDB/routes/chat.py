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
from faq_matcher import find_best_faq_match, find_faq_candidates
from gemini_client import create_chat, send_message_with_retry, send_message_with_image_retry
from moderation import check_violation
from image_handler import validate_image, save_image, build_image_url, prepare_image_for_gemini
from security import (
    detect_prompt_injection,
    PROMPT_INJECTION_MESSAGE,
    detect_internal_data_request,
    INTERNAL_DATA_REQUEST_MESSAGE,
)
from conversation_memory import build_history_for_gemini
from config import RAG_TOP_K, RAG_MAX_CHARS_PER_FIELD

chat_bp = Blueprint('chat', __name__)

GREETING_TEXT = (
    "Chào bạn! Mình là trợ lý ảo của Python Master 2026. "
    "Bạn có thể chọn nhanh 1 câu hỏi bên dưới, hoặc gõ câu hỏi của bạn."
)


def _truncate(text, max_chars):
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _build_retrieval_context(candidates):
    """Đóng gói top FAQ liên quan làm retrieval context cho Gemini.

    Tối ưu token (mục 4 yêu cầu):
    - Khử trùng lặp theo id FAQ (candidates đôi khi có thể trùng nếu nhánh
      full-text search không trả kết quả và fallback lấy toàn bộ FAQ).
    - Cắt bớt mỗi field theo RAG_MAX_CHARS_PER_FIELD (config.py) để 1 FAQ có
      phần "xử lý tình huống" quá dài không chiếm hết ngân sách token của cả
      retrieval context.
    - Số lượng chunk đã được giới hạn từ trước bởi RAG_TOP_K khi gọi
      find_faq_candidates(limit=RAG_TOP_K) ở nơi gọi hàm này.
    """
    if not candidates:
        return None

    chunks = []
    seen_ids = set()
    for row in candidates:
        faq_id = row.get("id")
        if faq_id is not None and faq_id in seen_ids:
            continue
        seen_ids.add(faq_id)

        index = len(chunks) + 1
        chunks.append(
            f"FAQ {index}: Intent={_truncate(row.get('intent'), RAG_MAX_CHARS_PER_FIELD)}\n"
            f"Câu hỏi mẫu={_truncate(row.get('cau_hoi_mau'), RAG_MAX_CHARS_PER_FIELD)}\n"
            f"Xử lý={_truncate(row.get('xu_ly_tinh_huong'), RAG_MAX_CHARS_PER_FIELD)}\n"
            f"Trả lời chuẩn={_truncate(row.get('tra_loi_chuan'), RAG_MAX_CHARS_PER_FIELD)}"
        )
    return "\n\n".join(chunks) if chunks else None


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
                save_message(cursor, connection, conversation_id, 'model', model_reply)
                return jsonify({
                    "status": "success",
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "reply": model_reply,
                    "used_faq": True,
                })

            # 6. Không có FAQ khớp đủ ngưỡng -> retrieval top FAQ rồi mới hỏi Gemini
            db_messages = get_conversation_messages(cursor, conversation_id)
            history = build_history_for_gemini(cursor, connection, conversation_id, db_messages)
            candidates = find_faq_candidates(cursor, user_message, limit=RAG_TOP_K)
            retrieval_context = _build_retrieval_context(candidates)
            chat = create_chat(history, retrieval_context=retrieval_context)
            model_reply, kind = send_message_with_retry(chat, user_message)
            save_message(cursor, connection, conversation_id, 'model', model_reply)

            response_payload = {
                "status": "success",
                "user_id": user_id,
                "conversation_id": conversation_id,
                "reply": model_reply,
                "used_faq": False,
            }
            _apply_ai_result(cursor, connection, conversation_id, model_reply, kind, response_payload)

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
            # vì FAQ matcher chỉ so khớp câu hỏi dạng text mẫu, không xử lý được ảnh)
            db_messages = get_conversation_messages(cursor, conversation_id)
            history = build_history_for_gemini(cursor, connection, conversation_id, db_messages)
            chat = create_chat(history)
            # Resize ảnh trước khi gửi Gemini để giảm token/latency (mục 9 yêu cầu):
            # ảnh chụp màn hình/điện thoại có thể vượt xa độ phân giải model cần.
            resized_bytes, resized_mime = prepare_image_for_gemini(
                image_data["bytes"], image_data["mime_type"]
            )
            model_reply, kind = send_message_with_image_retry(
                chat, user_message, resized_bytes, resized_mime
            )
            save_message(cursor, connection, conversation_id, 'model', model_reply)

            response_payload = {
                "status": "success",
                "user_id": user_id,
                "conversation_id": conversation_id,
                "reply": model_reply,
                "image_url": image_url,
                "used_faq": False,
            }
            _apply_ai_result(cursor, connection, conversation_id, model_reply, kind, response_payload)

            return jsonify(response_payload)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        connection.close()
