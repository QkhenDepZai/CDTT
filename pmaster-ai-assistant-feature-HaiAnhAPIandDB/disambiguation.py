"""
Khử nhập nhằng ngữ nghĩa cho các cụm từ dễ "bẫy" chatbot (D1-06).

Ví dụ kinh điển: "điểm thi" có thể là ĐIỂM SỐ (kết quả bài thi) hoặc ĐỊA ĐIỂM
thi. Embedding và từ khoá đều thấy 2 nghĩa rất gần nhau -> dễ trả lời lạc đề.

Cách làm (chạy TRƯỚC FAQ matcher và RAG, không tốn lệnh gọi AI nào):
1. Câu hỏi chứa 1 cụm đa nghĩa (trigger) -> chấm điểm từng nghĩa dựa trên
   các từ tín hiệu xung quanh ("bao nhiêu điểm", "tra cứu" -> điểm số;
   "ở đâu", "trường nào" -> địa điểm). Tín hiệu mạnh = 2 điểm, yếu = 1 điểm.
2. Một nghĩa thắng rõ ràng -> trả về truy vấn đã mở rộng + gợi ý ngữ cảnh
   cho RAG/Gemini, đồng thời BỎ QUA FAQ khớp từ khoá (chính là chỗ dễ bị bẫy).
3. Không có tín hiệu / hoà điểm -> hỏi lại người dùng kèm lựa chọn, KHÔNG đoán.
   Lượt sau, câu trả lời ("điểm số") được ghép với câu hỏi gốc để giải quyết.

So khớp trên văn bản đã BỎ DẤU nên hiểu cả khi người dùng gõ không dấu
("diem thi o dau"). Thêm cụm đa nghĩa mới = thêm 1 AmbiguityRule vào RULES.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def strip_accents(text: str) -> str:
    """'Địa điểm thi Ở ĐÂU?' -> 'dia diem thi o dau?' (chuẩn hoá để so khớp)."""
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text.replace("đ", "d")).strip()


@dataclass
class Sense:
    key: str
    label: str                      # hiển thị cho người dùng khi hỏi lại
    expansion: str                  # cụm từ thêm vào truy vấn RAG
    strong: list[str] = field(default_factory=list)   # regex (không dấu), +2 điểm
    weak: list[str] = field(default_factory=list)     # regex (không dấu), +1 điểm
    # Cụm gần như chắc chắn thuộc nghĩa này dù có tín hiệu khác đi kèm,
    # ví dụ "XEM ĐIỂM ở đâu" vẫn là điểm số dù có "ở đâu". +3 điểm.
    decisive: list[str] = field(default_factory=list)

    def score(self, plain_text: str) -> int:
        total = sum(3 for p in self.decisive if re.search(p, plain_text))
        total += sum(2 for p in self.strong if re.search(p, plain_text))
        return total + sum(1 for p in self.weak if re.search(p, plain_text))


@dataclass
class AmbiguityRule:
    key: str
    trigger: str                    # regex (không dấu) phát hiện cụm đa nghĩa
    question: str                   # câu hỏi lại khi không xác định được
    senses: list[Sense]


@dataclass
class Resolution:
    rule: str | None = None
    sense: Sense | None = None
    needs_clarification: bool = False
    clarification: str | None = None
    options: list[str] = field(default_factory=list)
    retrieval_query: str | None = None
    intent_hint: str | None = None

    @property
    def applied(self) -> bool:
        """True nếu câu hỏi chứa cụm đa nghĩa (đã giải quyết hoặc cần hỏi lại)."""
        return self.rule is not None


RULES = [
    AmbiguityRule(
        key="diem_thi",
        # "điểm" đứng riêng, KHÔNG phải một phần của "địa điểm".
        trigger=r"(?<!dia )\bdiem\b",
        question="Bạn muốn hỏi về điểm số (kết quả bài thi) hay địa điểm tổ chức thi ạ?",
        senses=[
            Sense(
                key="score",
                label="Điểm số / kết quả bài thi",
                expansion="điểm số, kết quả bài thi, cách chấm điểm, bảng xếp hạng",
                decisive=[r"xem diem", r"tra cuu", r"xem ket qua", r"diem so", r"bao nhieu diem",
                          r"may diem", r"phuc khao"],
                strong=[r"ket qua", r"cham diem", r"tinh diem", r"thang diem", r"diem chuan",
                        r"khieu nai", r"xep hang", r"\bbi sai\b", r"diem.{0,25}\b(sai|thap|cao)\b",
                        r"cos ?pro", r"chung chi", r"duoc bao nhieu", r"bai thi"],
                weak=[r"\bdat\b", r"\bdo\b", r"\btruot\b", r"\brot\b", r"cong bo", r"khi nao co",
                      r"bao gio co"],
            ),
            Sense(
                key="location",
                label="Địa điểm tổ chức thi",
                expansion="địa điểm thi, nơi tổ chức thi, thi ở đâu",
                decisive=[r"dia diem", r"dia chi", r"truong nao", r"diem thi so", r"noi thi",
                          r"duong di"],
                strong=[r"o dau", r"cho nao", r"ha noi", r"hcm", r"ho chi minh", r"sai gon",
                        r"da nang", r"tinh nao", r"thanh pho", r"den thi", r"phong thi nao"],
                weak=[r"\bgan\b", r"\bxa\b", r"\bdi\b"],
            ),
        ],
    ),
    AmbiguityRule(
        key="tai_khoan",
        trigger=r"\btai khoan\b",
        question="Bạn đang hỏi về tài khoản ôn luyện (SotaUni) hay tài khoản đăng ký dự thi ạ?",
        senses=[
            Sense(
                key="practice",
                label="Tài khoản ôn luyện SotaUni",
                expansion="tài khoản ôn luyện miễn phí trên nền tảng SotaUni",
                strong=[r"on luyen", r"sotauni", r"luyen tap", r"luyen thi", r"lam bai thu",
                        r"thi thu", r"hoc thu"],
            ),
            Sense(
                key="registration",
                label="Tài khoản đăng ký dự thi",
                expansion="tài khoản đăng ký dự thi trên hệ thống cuộc thi",
                strong=[r"dang ky (du )?thi", r"du thi", r"he thong thi", r"so bao danh"],
                weak=[r"dang nhap", r"\botp\b", r"mat khau", r"\bkhoa\b", r"dang ky"],
            ),
        ],
    ),
    AmbiguityRule(
        key="lich",
        trigger=r"\blich\b",
        question="Bạn muốn xem lịch thi chính thức hay lịch các buổi ôn luyện Python Prep Series ạ?",
        senses=[
            Sense(
                key="exam",
                label="Lịch thi chính thức",
                expansion="lịch thi, lịch trình cuộc thi, vòng loại, chung kết",
                strong=[r"lich thi", r"vong loai", r"chung ket", r"ca thi", r"so bao danh",
                        r"lich trinh cuoc thi", r"moc thoi gian", r"dang ky"],
            ),
            Sense(
                key="prep",
                label="Lịch Python Prep Series",
                expansion="lịch Python Prep Series, các buổi talkshow ôn luyện",
                strong=[r"prep", r"talkshow", r"buoi on", r"on luyen", r"livestream"],
            ),
        ],
    ),
]


def resolve(text: str, previous_text: str | None = None) -> Resolution:
    """Xác định nghĩa của câu hỏi. previous_text: câu hỏi gốc của người dùng
    khi lượt trước bot vừa HỎI LẠI (người dùng đang trả lời câu hỏi làm rõ)."""
    combined = f"{previous_text} {text}" if previous_text else text
    plain = strip_accents(combined)

    for rule in RULES:
        if not re.search(rule.trigger, plain):
            continue
        scores = sorted(((sense.score(plain), sense) for sense in rule.senses),
                        key=lambda item: item[0], reverse=True)
        best_score, best = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else 0

        if best_score > 0 and best_score > runner_up:
            others = ", ".join(s.label.lower() for _, s in scores[1:])
            return Resolution(
                rule=rule.key,
                sense=best,
                retrieval_query=f"{combined} ({best.expansion})",
                intent_hint=(f"Người dùng đang hỏi về: {best.label.lower()} "
                             f"(KHÔNG phải {others}). Chỉ trả lời đúng chủ đề này."),
            )
        # Không có tín hiệu nào, hoặc các nghĩa hoà điểm -> hỏi lại.
        return Resolution(
            rule=rule.key,
            needs_clarification=True,
            clarification=rule.question,
            options=[sense.label for sense in rule.senses],
        )
    return Resolution()
