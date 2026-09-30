"""Giờ trực, nhãn hiển thị và kiểm tra form để lại thông tin (D1-09)."""
from datetime import datetime, timezone

import pytest

import config
import handover
from chat_service import split_clarification


@pytest.fixture()
def hours(monkeypatch):
    def apply(spec, days="1-5"):
        monkeypatch.setattr(config, "SUPPORT_HOURS", spec)
        monkeypatch.setattr(config, "SUPPORT_DAYS", days)
        monkeypatch.setattr(config, "SUPPORT_UTC_OFFSET_HOURS", 7)
    return apply


def _utc(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def test_empty_hours_means_always_available(hours):
    hours("")
    assert handover.is_within_support_hours(_utc(27, 20))  # Chủ nhật, nửa đêm VN
    assert handover.support_hours_label() == "24/7"


def test_office_hours_in_vietnam_time(hours):
    hours("08:00-17:30")
    assert handover.is_within_support_hours(_utc(30, 1))        # Thứ 4 08:00 VN
    assert not handover.is_within_support_hours(_utc(30, 0, 59))  # 07:59 VN
    assert not handover.is_within_support_hours(_utc(30, 10, 30))  # 17:30 VN
    assert not handover.is_within_support_hours(_utc(27, 3))    # Chủ nhật
    assert handover.support_hours_label() == "08:00–17:30, Thứ 2–Thứ 6"


def test_overnight_shift(hours):
    hours("22:00-06:00", "1-7")
    assert handover.is_within_support_hours(_utc(30, 16))       # 23:00 VN
    assert not handover.is_within_support_hours(_utc(30, 5))    # 12:00 VN


def test_day_list_label(hours):
    hours("08:00-12:00", "7")
    assert handover.support_hours_label() == "08:00–12:00, Chủ nhật"


VALID_TICKET = {"full_name": "Nguyễn Văn A", "email": "a@example.com", "phone": "",
                "content": "Cần đổi bảng thi", "consent": True}


def test_valid_ticket_is_normalized():
    ticket, error = handover.validate_ticket({**VALID_TICKET, "phone": "0912 345 678"})
    assert error is None
    assert ticket == {"full_name": "Nguyễn Văn A", "email": "a@example.com",
                      "phone": "0912345678", "content": "Cần đổi bảng thi"}


@pytest.mark.parametrize("change, message", [
    ({"consent": False}, "đồng ý"),
    ({"consent": "true"}, "đồng ý"),
    ({"full_name": " "}, "họ tên"),
    ({"email": "", "phone": ""}, "email hoặc số điện thoại"),
    ({"email": "khong-phai-email"}, "Email không hợp lệ"),
    ({"phone": "12345"}, "Số điện thoại không hợp lệ"),
    ({"content": ""}, "nội dung"),
])
def test_invalid_ticket(change, message):
    ticket, error = handover.validate_ticket({**VALID_TICKET, **change})
    assert ticket is None and message in error


def test_split_clarification_from_gemini():
    question, options = split_clarification(
        "Bạn muốn hỏi lịch thi của bảng nào?\n- **Bảng A**\n- Bảng B\n* Cả hai bảng")
    assert question == "Bạn muốn hỏi lịch thi của bảng nào?"
    assert options == ["Bảng A", "Bảng B", "Cả hai bảng"]
