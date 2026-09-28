import logging

from google.genai import types
from gemini_client import client
from config import GEMINI_MODERATION_MODEL_NAME

logger = logging.getLogger("pmaster.moderation")

# Ngưỡng chặn chặt hơn dùng riêng cho bước kiểm duyệt (trước khi xử lý nghiệp vụ),
# khác với safety_settings dùng khi trả lời (gemini_client.py)
MODERATION_SAFETY_SETTINGS = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
]


def check_violation(text: str):
    """
    Kiểm tra nội dung có vi phạm không, TRƯỚC khi lưu/xử lý nghiệp vụ.
    Trả về (is_violation: bool, reason: str | None).
    Nếu bản thân bước kiểm duyệt bị lỗi (mạng, quota...), không chặn -
    để luồng chính tiếp tục xử lý bình thường, tránh chặn nhầm người dùng vì lỗi hạ tầng.

    Dùng model Flash-Lite (GEMINI_MODERATION_MODEL_NAME, config.py) vì đây chỉ
    là bước chấm điểm an toàn 1 câu hỏi ngắn, không cần model mạnh/đắt.
    Đây là request "phụ" (không phải câu trả lời chính), nên KHÔNG áp dụng
    retry nhiều lần như gemini_client._send_with_retry - nếu lỗi, bỏ qua bước
    chặn ngay (đúng hành vi cũ), tránh làm chậm luồng chat chính vì 1 bước
    kiểm duyệt không thiết yếu.
    """
    try:
        response = client.models.generate_content(
            model=GEMINI_MODERATION_MODEL_NAME,
            contents=text,
            config=types.GenerateContentConfig(
                safety_settings=MODERATION_SAFETY_SETTINGS,
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                max_output_tokens=50,
            ),
        )
    except Exception as e:
        logger.warning("[Moderation] Lỗi khi kiểm tra vi phạm, bỏ qua bước chặn: %s", e)
        return False, None

    feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(feedback, "block_reason", None) if feedback else None

    if block_reason:
        return True, str(block_reason)
    return False, None
