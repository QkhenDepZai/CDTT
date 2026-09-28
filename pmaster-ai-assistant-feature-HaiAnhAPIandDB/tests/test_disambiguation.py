import pytest

from disambiguation import resolve, strip_accents


@pytest.mark.parametrize("question,expected", [
    # "điểm thi" mơ hồ -> hỏi lại
    ("Em muốn biết điểm thi", "clarify"),
    ("Điểm thi", "clarify"),
    ("cho em hỏi về điểm thi với ạ", "clarify"),
    # điểm số
    ("Xem điểm thi ở đâu?", "score"),
    ("Tra cứu điểm thi vòng loại ở đâu", "score"),
    ("Điểm thi của em bị sai", "score"),
    ("Bao nhiêu điểm thì được cấp COS Pro?", "score"),
    ("Khi nào có điểm thi?", "score"),
    ("Mình được mấy điểm vậy", "score"),
    ("Cách chấm điểm như thế nào?", "score"),
    ("muon phuc khao diem thi", "score"),
    # địa điểm
    ("Điểm thi ở Hà Nội là trường nào?", "location"),
    ("diem thi o dau vay ad", "location"),
    ("Điểm thi Đà Nẵng ở chỗ nào", "location"),
    ("Điểm thi số 3 ở đâu", "location"),
    # tài khoản
    ("Tài khoản của em", "clarify"),
    ("Quên mật khẩu tài khoản ôn luyện", "practice"),
    ("Tài khoản đăng ký dự thi bị khóa", "registration"),
    # lịch
    ("Có lịch không ạ?", "clarify"),
    ("Lịch thi vòng loại khi nào", "exam"),
    ("Lịch các buổi Prep Series", "prep"),
])
def test_resolve(question, expected):
    result = resolve(question)
    actual = "clarify" if result.needs_clarification else result.sense.key
    assert actual == expected


def test_unambiguous_questions_are_untouched():
    for question in ("Địa điểm thi ở đâu?", "Lệ phí thi bao nhiêu?", "Bảng A dành cho ai?"):
        assert not resolve(question).applied


def test_clarification_answer_is_combined_with_original_question():
    result = resolve("Điểm số / kết quả bài thi", previous_text="Em muốn biết điểm thi")
    assert result.sense.key == "score"
    assert "Em muốn biết điểm thi" in result.retrieval_query
    assert "KHÔNG phải địa điểm" in result.intent_hint


def test_messenger_truncated_quick_reply_still_resolves():
    # Messenger cắt tiêu đề quick reply còn 20 ký tự.
    assert resolve("Điểm số / kết quả bà", previous_text="Điểm thi").sense.key == "score"
    assert resolve("Tài khoản đăng ký dự", previous_text="Tài khoản").sense.key == "registration"


def test_strip_accents():
    assert strip_accents("Địa ĐIỂM thi Ở  đâu?") == "dia diem thi o dau?"
