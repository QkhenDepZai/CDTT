"""
Xác định thí sinh thuộc Bảng A hay Bảng B (D1-06, TC-CLARIFY-01..03, TC-CONTEXT-01..03).

Câu "Em thuộc bảng nào?" không thể trả lời nếu chưa biết tuổi / cấp học ->
chatbot phải HỎI LẠI (kèm lựa chọn nhanh), không được đoán. Khi người dùng
bổ sung ("Em 15 tuổi"), câu trả lời được ghép với câu hỏi gốc để kết luận.

Làm bằng luật tất định thay vì để Gemini tự suy luận vì:
- quy định độ tuổi là dữ liệu chuẩn của BTC (config.TRACK_*), tính tuổi từ
  năm sinh là phép trừ - không có lý do chấp nhận rủi ro AI tính sai;
- trả lời tức thì, không tốn quota, QC kiểm thử được kết quả lặp lại.

    advice = advise("Em sinh năm 2010 thì thi bảng nào?")
    advice.reply                -> "... thuộc nhóm đối tượng của **Bảng A** ..."
    advise("Em thuộc bảng nào vậy?").needs_clarification -> True
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from config import (
    COMPETITION_YEAR,
    SUPPORT_CONTACT_TEXT,
    TRACK_A_MAX_AGE,
    TRACK_A_MIN_AGE,
    TRACK_B_MAX_AGE,
    TRACK_B_MIN_AGE,
)
from text_utils import strip_accents

INTENT = "xac_dinh_bang_thi"

# Mọi mẫu viết trên văn bản đã bỏ dấu (text_utils.strip_accents).
TRIGGER = re.compile(r"\bbang nao\b|\bbang a hay (bang )?b\b|\bthuoc bang\b|\bthi bang gi\b")
AGE = re.compile(r"\b(\d{1,2})\s*(?:tuoi|t)\b")
# "13–18 tuổi" là KHOẢNG tuổi (vd chữ trên nút gợi ý), không phải tuổi người dùng.
AGE_RANGE = re.compile(r"\b\d{1,2}\s*[-–~]\s*\d{1,2}\s*(?:tuoi|t)\b")
BIRTH_YEAR = re.compile(r"\b(?:sinh|sn)\D{0,10}((?:19|20)\d{2})\b")
BIRTH_YEAR_SLANG = re.compile(r"\b2k(\d{1,2})\b")  # "em 2k10" = sinh năm 2010
GRADE = re.compile(r"\blop\s*(\d{1,2})\b")
LEVEL_A = re.compile(r"\b(thcs|thpt|cap (2|3|hai|ba)|hoc sinh|trung hoc)\b")
LEVEL_B = re.compile(r"\b(sinh vien|dai hoc|cao dang|dh|cd|nam (nhat|hai|ba|tu|cuoi))\b")

CLARIFICATION_QUESTION = (
    "Để xác định bạn thuộc Bảng A hay Bảng B, bạn vui lòng cho biết thêm "
    "độ tuổi hoặc năm sinh của mình nhé."
)
CLARIFICATION_OPTIONS = [
    f"Học sinh THCS/THPT ({TRACK_A_MIN_AGE}–{TRACK_A_MAX_AGE} tuổi)",
    f"Sinh viên ĐH/CĐ ({TRACK_B_MIN_AGE}–{TRACK_B_MAX_AGE} tuổi)",
]

TRACK_DESCRIPTIONS = {
    "A": (f"Bảng A dành cho học sinh THCS và THPT (độ tuổi từ {TRACK_A_MIN_AGE} đến "
          f"{TRACK_A_MAX_AGE} tuổi), nội dung thi tập trung vào kiến thức Python cơ bản."),
    "B": (f"Bảng B dành cho sinh viên Cao đẳng, Đại học (độ tuổi từ {TRACK_B_MIN_AGE} đến "
          f"{TRACK_B_MAX_AGE} tuổi), nội dung thi tập trung vào lập trình Python nâng cao."),
}


@dataclass
class Profile:
    age: int | None = None
    birth_year: int | None = None
    grade: int | None = None
    level: str | None = None  # "A" | "B" theo cấp học

    @property
    def is_empty(self) -> bool:
        return self.age is None and self.grade is None and self.level is None


@dataclass
class TrackAdvice:
    applies: bool = False
    track: str | None = None          # "A" | "B" | None (ngoài độ tuổi dự thi)
    reply: str | None = None
    needs_clarification: bool = False
    clarification: str | None = None
    options: list[str] = field(default_factory=list)


def _last_int(pattern: re.Pattern, text: str) -> int | None:
    matches = pattern.findall(text)
    if not matches:
        return None
    last = matches[-1]
    return int(last[0] if isinstance(last, tuple) else last)


def extract_profile(text: str) -> Profile:
    """Lấy tuổi / năm sinh / lớp / cấp học. Nếu có nhiều giá trị, dùng giá trị
    cuối cùng (thông tin người dùng vừa bổ sung ghi đè thông tin cũ)."""
    plain = strip_accents(text)
    profile = Profile()

    birth_year = _last_int(BIRTH_YEAR, plain)
    slang = _last_int(BIRTH_YEAR_SLANG, plain)
    if birth_year is None and slang is not None:
        birth_year = 2000 + slang
    if birth_year is not None and 1950 < birth_year <= COMPETITION_YEAR:
        profile.birth_year = birth_year
        profile.age = COMPETITION_YEAR - birth_year

    age = _last_int(AGE, AGE_RANGE.sub(" ", plain))
    if age is not None and 3 <= age <= 99:
        profile.age = age
        profile.birth_year = None

    grade = _last_int(GRADE, plain)
    if grade is not None and 1 <= grade <= 12:
        profile.grade = grade

    if LEVEL_B.search(plain):
        profile.level = "B"
    elif LEVEL_A.search(plain) or profile.grade is not None:
        profile.level = "A"
    return profile


def _describe(profile: Profile) -> str:
    if profile.birth_year is not None:
        return f"sinh năm {profile.birth_year} ({profile.age} tuổi trong năm {COMPETITION_YEAR})"
    if profile.age is not None:
        return f"{profile.age} tuổi"
    if profile.grade is not None:
        return f"đang học lớp {profile.grade}"
    return "là học sinh THCS/THPT" if profile.level == "A" else "là sinh viên Cao đẳng/Đại học"


def _track_by_age(age: int) -> str | None:
    if TRACK_A_MIN_AGE <= age <= TRACK_A_MAX_AGE:
        return "A"
    if TRACK_B_MIN_AGE <= age <= TRACK_B_MAX_AGE:
        return "B"
    return None


def decide(profile: Profile) -> TrackAdvice:
    """Kết luận bảng thi từ hồ sơ đã có ít nhất 1 thông tin. Tuổi là tiêu chí
    chính thức nên được ưu tiên hơn cấp học khi cả hai cùng có."""
    who = _describe(profile)
    track = _track_by_age(profile.age) if profile.age is not None else profile.level
    if track is None:
        reply = (
            f"Theo quy định, Python Master dành cho thí sinh từ {TRACK_A_MIN_AGE} đến "
            f"{TRACK_B_MAX_AGE} tuổi (Bảng A: {TRACK_A_MIN_AGE}–{TRACK_A_MAX_AGE} tuổi, "
            f"Bảng B: {TRACK_B_MIN_AGE}–{TRACK_B_MAX_AGE} tuổi). Với thông tin bạn {who}, "
            f"bạn chưa nằm trong độ tuổi dự thi. Nếu cần hỗ trợ thêm, bạn vui lòng liên hệ "
            f"Ban tổ chức ({SUPPORT_CONTACT_TEXT}) hoặc bấm \"Gặp tư vấn viên\"."
        )
        return TrackAdvice(applies=True, track=None, reply=reply)

    reply = (
        f"Theo quy định về đối tượng tham gia của Python Master, nếu bạn {who} thì bạn "
        f"thuộc nhóm đối tượng của **Bảng {track}**.\n\n{TRACK_DESCRIPTIONS[track]}"
    )
    return TrackAdvice(applies=True, track=track, reply=reply)


def advise(text: str, previous_text: str | None = None) -> TrackAdvice:
    """previous_text: câu hỏi gốc khi lượt trước bot vừa HỎI LẠI (người dùng
    đang trả lời câu hỏi làm rõ). Không liên quan tới bảng thi -> applies=False."""
    combined = f"{previous_text}\n{text}" if previous_text else (text or "")
    if not TRIGGER.search(strip_accents(combined)):
        return TrackAdvice()

    profile = extract_profile(combined)
    if not profile.is_empty:
        return decide(profile)
    if previous_text:
        # Đã hỏi lại 1 lần mà người dùng vẫn không cho biết -> không hỏi mãi,
        # để luồng FAQ/RAG thông thường xử lý câu trả lời mới.
        return TrackAdvice()
    return TrackAdvice(
        applies=True,
        needs_clarification=True,
        clarification=CLARIFICATION_QUESTION,
        options=list(CLARIFICATION_OPTIONS),
    )
