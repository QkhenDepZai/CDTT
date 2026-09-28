import re
from flask import jsonify, request


# Các mẫu phổ biến của Prompt Injection. Đây là lớp chặn sớm, không thay thế
# moderation/safety của model.
PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(all|any|previous|prior)\s+(instructions|rules|prompts)",
    r"bỏ\s+qua\s+(mọi|tất cả|toàn bộ)\s+(hướng\s+dẫn|quy\s+tắc|chỉ\s+dẫn)",
    r"bỏ\s+qua\s+(system\s+prompt|system\s+instruction|quy\s+tắc\s+hệ\s+thống)",
    r"reveal\s+(the\s+)?(system\s+prompt|hidden\s+prompt|instructions)",
    r"show\s+(me\s+)?(the\s+)?(system\s+prompt|hidden\s+instructions)",
    r"in ra\s+(system\s+prompt|prompt\s+ẩn|cấu\s+hình\s+ẩn)",
    r"print\s+(system\s+prompt|hidden\s+prompt)",
    r"developer\s+message",
    r"system\s+message",
    r"jailbreak",
    r"do\s+anything\s+now",
]

_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in PROMPT_INJECTION_PATTERNS]


PROMPT_INJECTION_MESSAGE = (
    "Xin lỗi, mình không thể thực hiện yêu cầu thay đổi hoặc tiết lộ "
    "cấu hình/hướng dẫn nội bộ của hệ thống. Bạn có thể hỏi về Python Master 2026 "
    "hoặc kiến thức Python trong phạm vi hỗ trợ."
)


def detect_prompt_injection(text: str):
    """Trả về (True, matched_pattern) nếu phát hiện dấu hiệu prompt injection."""
    value = (text or "").strip()
    for pattern in _COMPILED_PATTERNS:
        if pattern.search(value):
            return True, pattern.pattern
    return False, None


# ============================================================
# CHẶN YÊU CẦU LẤY DỮ LIỆU NỘI BỘ / TỔNG HỢP SỐ LIỆU NGƯỜI DÙNG
# ============================================================
# Đây là lớp chặn RIÊNG, khác với detect_prompt_injection() (chặn kiểu "bỏ qua
# hướng dẫn") và moderation.check_violation() (chặn nội dung độc hại/bạo lực/
# tình dục...). Loại câu hỏi này (ví dụ: "Tổng hợp số người đăng ký thi bảng A")
# không khớp pattern injection, cũng không phải nội dung độc hại -> trước đây
# LỌT QUA CẢ 2 LỚP TRÊN, để hệ thống FAQ-matching/Gemini tự "may rủi" quyết
# định có trả lời hay không (đã có case match nhầm sang 1 FAQ không liên quan
# và trả lời trực tiếp, dù may mắn là chưa lộ số liệu thật).
#
# Chặn SỚM, TRƯỚC KHI vào FAQ matcher hay Gemini, thay vì trông chờ vào việc
# FAQ không match / Gemini tự chối - vì cả 2 đều không đảm bảo 100%: FAQ có
# thể match nhầm (dựa trên điểm số, không phải luật cứng), còn Gemini hiện
# KHÔNG có tool/database access nên không thể lấy số liệu thật, nhưng vẫn có
# rủi ro tự bịa ra một con số (hallucination) nếu không bị chặn dứt khoát từ
# tầng ứng dụng.
INTERNAL_DATA_REQUEST_PATTERNS = [
    # tổng hợp / thống kê / liệt kê / danh sách ... người đăng ký, thí sinh...
    r"(tổng\s*hợp|thống\s*kê|liệt\s*kê|danh\s*sách|xuất)\s+.*(người\s*đăng\s*ký|thí\s*sinh|người\s*dùng|tài\s*khoản|học\s*sinh|sinh\s*viên)",
    # bao nhiêu / số lượng người đã đăng ký
    r"(bao\s*nhiêu|số\s*lượng|số\s*người)\s+.*(đăng\s*ký|thí\s*sinh|tham\s*gia)",
    # xin trực tiếp thông tin cá nhân/liên hệ của thí sinh khác
    r"(thông\s*tin\s*cá\s*nhân|email|số\s*điện\s*thoại|sđt|địa\s*chỉ)\s+.*(thí\s*sinh|người\s*dùng|học\s*sinh|sinh\s*viên)",
    # truy vấn thẳng cơ sở dữ liệu / bảng dữ liệu
    r"(database|cơ\s*sở\s*dữ\s*liệu|csdl|truy\s*vấn\s*sql|bảng\s*dữ\s*liệu)\s+.*(thí\s*sinh|người\s*dùng|đăng\s*ký)",
]

_COMPILED_INTERNAL_DATA_PATTERNS = [re.compile(p, re.IGNORECASE) for p in INTERNAL_DATA_REQUEST_PATTERNS]

INTERNAL_DATA_REQUEST_MESSAGE = (
    "Xin lỗi, mình không có quyền truy cập hoặc cung cấp số liệu/thông tin nội bộ "
    "về người dùng, thí sinh hay số lượng đăng ký. Nếu bạn cần thông tin thống kê "
    "chính thức, vui lòng liên hệ Ban Tổ Chức hoặc tư vấn viên."
)


def detect_internal_data_request(text: str):
    """Trả về (True, matched_pattern) nếu câu hỏi có dấu hiệu xin số liệu tổng
    hợp/thống kê/danh sách người dùng - thí sinh, KHÔNG phải câu hỏi nghiệp vụ
    thông thường về nội dung/thể lệ cuộc thi."""
    value = (text or "").strip()
    for pattern in _COMPILED_INTERNAL_DATA_PATTERNS:
        if pattern.search(value):
            return True, pattern.pattern
    return False, None


def get_request_site_key():
    """Đọc site_key từ JSON/form hoặc header X-Site-Key."""
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form or {}
    return data.get("site_key") or request.headers.get("X-Site-Key")


def require_staff_role(get_db_connection, get_user_role):
    """Decorator-like helper cho route staff; trả (error_response, staff_id)."""
    data = request.get_json(silent=True) or {}
    staff_id = data.get("staff_id") or request.headers.get("X-Staff-Id")
    if not staff_id:
        return jsonify({"error": "Thiếu staff_id"}), None

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            role = get_user_role(cursor, staff_id)
        if role != "staff":
            return jsonify({"error": "Tài khoản không có quyền tư vấn viên"}), None
    finally:
        connection.close()

    return None, int(staff_id)
