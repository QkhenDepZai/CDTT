"""Xác định Bảng A/B và hỏi lại khi thiếu thông tin (D1-06, TC-CLARIFY, TC-CONTEXT)."""
import pytest

from track_advisor import CLARIFICATION_OPTIONS, advise, extract_profile

ASKED = "Em thuộc bảng nào vậy?"


def test_missing_info_asks_back_with_quick_replies():
    advice = advise(ASKED)
    assert advice.needs_clarification and advice.track is None
    assert "độ tuổi hoặc năm sinh" in advice.clarification
    assert advice.options == CLARIFICATION_OPTIONS


@pytest.mark.parametrize("text, previous, track", [
    ("Em 15 tuổi", ASKED, "A"),                          # TC-CLARIFY-03 / TC-CONTEXT-01
    ("15 tuổi thì thi bảng nào thế?", None, "A"),         # TC-FAQ-14
    ("Em sinh năm 2010 thì thi bảng nào?", None, "A"),    # TC-CONTEXT-02
    ("Học sinh cấp 3 thì thi bảng nào?", None, "A"),      # TC-CONTEXT-03
    ("22 tuổi đăng ký bảng nào được?", None, "B"),
    ("Sinh viên đại học thi bảng nào?", None, "B"),
    ("em 2k8 thi bảng a hay b", None, "A"),
    (CLARIFICATION_OPTIONS[0], ASKED, "A"),               # bấm nút gợi ý
    (CLARIFICATION_OPTIONS[1], ASKED, "B"),
])
def test_decides_track(text, previous, track):
    advice = advise(text, previous)
    assert advice.applies and not advice.needs_clarification
    assert advice.track == track
    assert f"**Bảng {track}**" in advice.reply


def test_option_age_range_is_not_read_as_user_age():
    advice = advise(CLARIFICATION_OPTIONS[1], ASKED)
    assert "24 tuổi thì" not in advice.reply and "sinh viên" in advice.reply


def test_out_of_age_range_is_not_eligible():
    advice = advise("Em 11 tuổi thi bảng nào?")
    assert advice.applies and advice.track is None
    assert "chưa nằm trong độ tuổi dự thi" in advice.reply


@pytest.mark.parametrize("text, previous", [
    ("Bảng A thi gì?", None),                  # không hỏi "bảng nào" -> để FAQ/RAG
    ("Em 15 tuổi", None),                      # không có ngữ cảnh hỏi bảng
    ("không biết nữa", ASKED),                 # đã hỏi lại 1 lần -> không hỏi mãi
])
def test_not_applied(text, previous):
    assert not advise(text, previous).applies


def test_birth_year_age_uses_competition_year():
    profile = extract_profile("em sinh năm 2008")
    assert profile.birth_year == 2008 and profile.age == 18
