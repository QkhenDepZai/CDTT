"""
Lớp kiểm soát nội dung CHẠY TRƯỚC FAQ/RAG/Gemini (D1-07, D1-08, D1-13).

Toàn bộ là luật tất định (regex + danh mục từ khoá), không tốn lệnh gọi AI
nào nên nhanh và cho kết quả lặp lại được khi QC kiểm thử. Bước kiểm duyệt
bằng Gemini (moderation.check_violation) vẫn chạy sau đó như lớp thứ hai.

    result = check_message(text)
    if result.blocked: ...   # result.category cho biết loại vi phạm

Thứ tự kiểm tra:
    1. prompt_injection       yêu cầu bỏ qua quy tắc / đổi vai / xin system prompt
    2. internal_data_request  xin dữ liệu nội bộ: KB, cấu hình, API key,
                              số liệu hoặc thông tin cá nhân của người khác
    3. banned_keyword         từ khoá cấm/nhạy cảm (data/banned_keywords.txt)
    4. out_of_scope           chủ đề xã hội hiển nhiên ngoài phạm vi (thời tiết,
                              giá vàng...) hoặc hỏi lập trình ngôn ngữ khác Python.
                              Trường hợp tinh tế hơn do Gemini quyết định qua
                              marker [NGOAI_PHAM_VI].

Mọi trường hợp bị chặn đều trả CÙNG MỘT câu mẫu an toàn (SAFE_REFUSAL_MESSAGE)
theo bảng "Xác định câu hỏi và tình huống" của BA: không tiết lộ hệ thống đã
nhận ra loại tấn công nào.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache

from text_utils import normalize_vi, strip_accents

logger = logging.getLogger("pmaster.guardrails")

SAFE_REFUSAL_MESSAGE = (
    "Các câu hỏi bạn đưa ra không liên quan đến thông tin của cuộc thi Python Master. "
    "Bạn còn câu hỏi gì cần giải đáp về cuộc thi Python Master không?"
)

BANNED_KEYWORDS_PATH = os.getenv(
    "BANNED_KEYWORDS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "banned_keywords.txt"),
)

PROMPT_INJECTION = "prompt_injection"
INTERNAL_DATA_REQUEST = "internal_data_request"
BANNED_KEYWORD = "banned_keyword"
OUT_OF_SCOPE = "out_of_scope"

# Mẫu viết trên văn bản đã normalize_vi (chữ thường, CÓ dấu).
PROMPT_INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget)\s+(all\s+|any\s+)?(the\s+|your\s+)?(previous|prior|above|earlier|system)?\s*(instructions|rules|prompts?)",
    r"(bỏ\s*qua|phớt\s*lờ|lờ\s*đi|không\s*(cần\s*)?tuân\s*(theo|thủ))\s+(đi\s+)?(mọi|tất\s*cả|toàn\s*bộ|các|những)\s+(hướng\s*dẫn|quy\s*tắc|chỉ\s*dẫn|luật|lệnh|ràng\s*buộc)",
    r"(bỏ\s*qua|phớt\s*lờ|lờ\s*đi)\s+(hướng\s*dẫn|quy\s*tắc|chỉ\s*dẫn|luật|lệnh|ràng\s*buộc)\s*(trước\s*đó|trước|ban\s*đầu|hệ\s*thống|của\s*bạn)",
    r"system\s*(prompt|message|instruction)",
    r"developer\s*(message|mode)",
    r"prompt\s*(hệ\s*thống|ẩn|gốc|nội\s*bộ|ban\s*đầu)",
    r"(hướng\s*dẫn|chỉ\s*dẫn)\s+(nội\s*bộ|ẩn|gốc)\s+(của\s*bạn)?",
    r"chế\s*độ\s*(nhà\s*phát\s*triển|developer|không\s*giới\s*hạn)",
    r"\bjailbreak\b",
    r"do\s+anything\s+now",
    r"\b(act\s+as|pretend\s+(to\s+be|you\s+are)|you\s+are\s+now)\b",
    r"(hãy\s+)?(đóng\s*vai|nhập\s*vai|giả\s*vờ\s*(là|làm))\s+",
]

INTERNAL_DATA_PATTERNS = [
    # Rò rỉ Knowledge Base / dữ liệu huấn luyện.
    r"(cho|gửi|in|xuất|liệt\s*kê|đưa|cung\s*cấp|show|dump)\s+(tôi\s+|mình\s+|em\s+|ra\s+)?(xem\s+)?(toàn\s*bộ|tất\s*cả|hết)\s+(nội\s*dung|dữ\s*liệu|tài\s*liệu|kiến\s*thức|câu\s*hỏi|faq)",
    r"knowledge\s*base|kho\s*(tri\s*thức|dữ\s*liệu)|dữ\s*liệu\s*huấn\s*luyện|training\s*data",
    # Cấu hình, khoá bí mật.
    r"(cấu\s*hình|thiết\s*lập)\s+(nội\s*bộ|hệ\s*thống|ẩn|của\s*bạn)",
    r"\bapi[\s_-]*key\b|(khóa|khoá)\s*(bí\s*mật|api)|\.env\b|access\s*token",
    r"(mật\s*khẩu|password)\s+(của\s+)?(database|cơ\s*sở\s*dữ\s*liệu|csdl|admin|quản\s*trị|hệ\s*thống)",
    # Số liệu tổng hợp về người đăng ký. Cố ý KHÔNG chặn câu hỏi thể lệ công
    # khai như "Lấy bao nhiêu thí sinh vào chung kết?" hay "Danh sách thí sinh
    # vào chung kết xem ở đâu?".
    r"(tổng\s*hợp|thống\s*kê|xuất)\s+.*(người\s*đăng\s*ký|thí\s*sinh|người\s*dùng|tài\s*khoản|học\s*sinh|sinh\s*viên)",
    r"(bao\s*nhiêu|số\s*lượng|tổng\s*số)\s+(người|thí\s*sinh|bạn|học\s*sinh|sinh\s*viên)\s+(đã\s+)?(đăng\s*ký|tham\s*gia|dự\s*thi)",
    r"danh\s*sách\s+(email|số\s*điện\s*thoại|sđt|tài\s*khoản|thông\s*tin|người\s*đăng\s*ký|người\s*dùng)",
    r"(database|cơ\s*sở\s*dữ\s*liệu|csdl|truy\s*vấn\s*sql|bảng\s*dữ\s*liệu)\s+.*(thí\s*sinh|người\s*dùng|đăng\s*ký)",
    # Thông tin cá nhân / tài khoản của người khác.
    r"(thông\s*tin(\s*cá\s*nhân)?|email|số\s*điện\s*thoại|sđt|địa\s*chỉ|mật\s*khẩu|tài\s*khoản|điểm|kết\s*quả|số\s*báo\s*danh)\s+(của\s+)?(các|những|mọi|tất\s*cả)\s+(thí\s*sinh|người\s*dùng|học\s*sinh|sinh\s*viên|người)",
    r"thông\s*tin\s+(của\s+)?(một\s+)?(thí\s*sinh|người\s*dùng|học\s*sinh|sinh\s*viên|tài\s*khoản)\s+(khác|tên)",
    r"của\s+(một\s+)?(thí\s*sinh|người\s*dùng|người|bạn|học\s*sinh|sinh\s*viên)\s+(khác|tên)\b",
]

# Mẫu viết trên văn bản đã strip_accents (không dấu) để bắt cả khi gõ không dấu.
OUT_OF_SCOPE_PATTERNS = [
    r"\bthoi tiet\b",
    r"\bgia vang\b",
    r"\bty gia\b",
    r"\bxo so\b|\bxsmb\b",
    r"\bchung khoan\b|\bgia co phieu\b",
    r"\bgia (bitcoin|btc|coin)\b",
    r"\bket qua bong da\b|\blich thi dau bong da\b",
    r"\b(boi bai|tu vi|cung hoang dao)\b",
    r"\bcong thuc nau\b|\bnau mon\b",
    r"\bviet (giup |ho )?(toi |minh |em )?(mot )?bai van\b",
    r"\bke (cho (toi|minh|em) )?(mot )?(cau )?chuyen\b",
]

# Hỏi lập trình bằng NGÔN NGỮ KHÁC Python (BA: "Chủ đề lập trình ngoài phạm
# vi"). Không chặn khi câu hỏi nói về cuộc thi, vd FAQ hợp lệ "Dùng C++ có
# tham gia được không?".
OTHER_LANGUAGE_PATTERN = r"(?<![\w+#])(c\+\+|c#|java|javascript|typescript|sql|mysql|php|golang|ruby|kotlin|swift|rust|pascal|html|css)(?![\w+#])"
COMPETITION_CONTEXT_PATTERN = r"\b(thi|du thi|tham gia|cuoc thi|python master|dang ky|ngon ngu)\b"


@dataclass(frozen=True)
class GuardrailResult:
    blocked: bool = False
    category: str | None = None
    detail: str | None = None  # mẫu/từ khoá khớp - chỉ ghi log, KHÔNG trả cho người dùng


def _compile(patterns):
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


_INJECTION = _compile(PROMPT_INJECTION_PATTERNS)
_INTERNAL = _compile(INTERNAL_DATA_PATTERNS)
_OUT_OF_SCOPE = _compile(OUT_OF_SCOPE_PATTERNS)
_OTHER_LANGUAGE = re.compile(OTHER_LANGUAGE_PATTERN)
_COMPETITION_CONTEXT = re.compile(COMPETITION_CONTEXT_PATTERN)


def _first_match(compiled, text):
    for pattern in compiled:
        if pattern.search(text):
            return pattern.pattern
    return None


def _keyword_pattern(keyword: str) -> re.Pattern:
    words = r"\s+".join(re.escape(part) for part in keyword.split())
    return re.compile(rf"(?<!\w){words}(?!\w)")


@lru_cache(maxsize=1)
def load_banned_keywords(path: str = BANNED_KEYWORDS_PATH):
    """Đọc danh mục từ khoá: trả về (mẫu khớp văn bản có dấu, mẫu khớp văn bản
    không dấu). Thiếu file -> chỉ cảnh báo, các lớp chặn khác vẫn chạy."""
    accented, plain = [], []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        logger.warning("[Guardrails] Không đọc được danh mục từ khoá cấm: %s", path)
        return (), ()
    for line in lines:
        keyword = normalize_vi(line)
        if not keyword or keyword.startswith("#"):
            continue
        (plain if keyword.isascii() else accented).append((keyword, _keyword_pattern(keyword)))
    return tuple(accented), tuple(plain)


def find_banned_keyword(text: str) -> str | None:
    return _first_keyword(text, *load_banned_keywords())


def _first_keyword(text: str, accented, plain) -> str | None:
    normalized = normalize_vi(text)
    for keyword, pattern in accented:
        if pattern.search(normalized):
            return keyword
    stripped = strip_accents(text)
    for keyword, pattern in plain:
        if pattern.search(stripped):
            return keyword
    return None


def check_message(text: str) -> GuardrailResult:
    """Kiểm tra 1 tin nhắn người dùng. Rỗng -> không chặn."""
    normalized = normalize_vi(text)
    if not normalized:
        return GuardrailResult()

    matched = _first_match(_INJECTION, normalized)
    if matched:
        return GuardrailResult(True, PROMPT_INJECTION, matched)
    matched = _first_match(_INTERNAL, normalized)
    if matched:
        return GuardrailResult(True, INTERNAL_DATA_REQUEST, matched)
    keyword = find_banned_keyword(text)
    if keyword:
        return GuardrailResult(True, BANNED_KEYWORD, keyword)
    plain = strip_accents(text)
    matched = _first_match(_OUT_OF_SCOPE, plain)
    if matched:
        return GuardrailResult(True, OUT_OF_SCOPE, matched)
    language = _OTHER_LANGUAGE.search(plain)
    if language and not _COMPETITION_CONTEXT.search(plain):
        return GuardrailResult(True, OUT_OF_SCOPE, f"other_language:{language.group(1)}")
    return GuardrailResult()
