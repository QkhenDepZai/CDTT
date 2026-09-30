"""
Chuyển hội thoại từ AI sang tư vấn viên (D1-09) - dùng chung cho mọi kênh.

    result = request_handover(cursor, connection, conversation_id, reason)
    result.mode == "live"         -> phiên vào hàng chờ (waiting_agent), bot im lặng,
                                     nhân viên nhận thông báo In-app + email
    result.mode == "after_hours"  -> ngoài giờ trực: bot tiếp tục hỗ trợ, thí sinh
                                     được mời để lại thông tin (support ticket)

Giờ trực cấu hình ở .env (SUPPORT_HOURS, SUPPORT_DAYS, SUPPORT_UTC_OFFSET_HOURS);
để trống SUPPORT_HOURS = luôn có người trực.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
import notifier
from database import (
    create_agent_notification,
    create_support_ticket,
    save_message,
    set_conversation_status,
)

LIVE = "live"
AFTER_HOURS = "after_hours"

AGENT_REQUESTED_MESSAGE = "Đã chuyển yêu cầu tới tư vấn viên, vui lòng chờ trong giây lát."
WEEKDAY_LABELS = {1: "Thứ 2", 2: "Thứ 3", 3: "Thứ 4", 4: "Thứ 5", 5: "Thứ 6", 6: "Thứ 7", 7: "Chủ nhật"}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_PATTERN = re.compile(r"^(\+84|0)\d{9,10}$")
MAX_NAME_CHARS = 100
MAX_CONTENT_CHARS = 2000


@dataclass(frozen=True)
class HandoverResult:
    mode: str
    message: str

    @property
    def after_hours(self) -> bool:
        return self.mode == AFTER_HOURS


# ---- Giờ trực ---------------------------------------------------------------------------
def _parse_clock(value: str) -> int:
    hours, minutes = value.strip().split(":")
    total = int(hours) * 60 + int(minutes)
    if not 0 <= total <= 24 * 60:
        raise ValueError(value)
    return total


def parse_support_hours(spec: str) -> tuple[int, int] | None:
    """"08:00-17:30" -> (480, 1050) phút trong ngày; rỗng -> None (luôn trực)."""
    if not spec:
        return None
    start, end = spec.replace("–", "-").split("-", 1)
    return _parse_clock(start), _parse_clock(end)


def parse_support_days(spec: str) -> set[int]:
    """"1-5" hoặc "1,2,3,4,5,6" -> tập ngày (1 = Thứ 2 ... 7 = Chủ nhật)."""
    days: set[int] = set()
    for part in (spec or "1-7").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            first, last = (int(x) for x in part.split("-", 1))
            days.update(range(first, last + 1))
        else:
            days.add(int(part))
    return {day for day in days if 1 <= day <= 7} or set(range(1, 8))


def _local_now(now_utc: datetime | None = None) -> datetime:
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(timezone(timedelta(hours=config.SUPPORT_UTC_OFFSET_HOURS)))


def is_within_support_hours(now_utc: datetime | None = None) -> bool:
    hours = parse_support_hours(config.SUPPORT_HOURS)
    if hours is None:
        return True
    local = _local_now(now_utc)
    if local.isoweekday() not in parse_support_days(config.SUPPORT_DAYS):
        return False
    minute_of_day = local.hour * 60 + local.minute
    start, end = hours
    if start <= end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end  # ca qua nửa đêm, vd 22:00-06:00


def support_hours_label() -> str:
    hours = parse_support_hours(config.SUPPORT_HOURS)
    if hours is None:
        return "24/7"
    clock = "–".join(f"{m // 60:02d}:{m % 60:02d}" for m in hours)
    days = sorted(parse_support_days(config.SUPPORT_DAYS))
    if days == list(range(1, 8)):
        return f"{clock} hằng ngày"
    if len(days) > 1 and days == list(range(days[0], days[-1] + 1)):
        return f"{clock}, {WEEKDAY_LABELS[days[0]]}–{WEEKDAY_LABELS[days[-1]]}"
    return f"{clock}, " + ", ".join(WEEKDAY_LABELS[day] for day in days)


def after_hours_message() -> str:
    return (
        "Hiện đã ngoài giờ làm việc của tư vấn viên (giờ trực: "
        f"{support_hours_label()}) nên chưa có nhân viên trực để hỗ trợ ngay. "
        "Bạn vui lòng để lại họ tên, email hoặc số điện thoại và nội dung cần hỗ trợ, "
        "tư vấn viên sẽ liên hệ lại trong giờ làm việc. Bạn cũng có thể liên hệ "
        f"Ban tổ chức: {config.SUPPORT_CONTACT_TEXT}."
    )


# ---- Chuyển tư vấn viên ---------------------------------------------------------------------
def request_handover(cursor, connection, conversation_id: int, reason: str,
                     now_utc: datetime | None = None) -> HandoverResult:
    if not is_within_support_hours(now_utc):
        message = after_hours_message()
        save_message(cursor, connection, conversation_id, 'system', message)
        return HandoverResult(AFTER_HOURS, message)

    set_conversation_status(cursor, connection, conversation_id, 'waiting_agent')
    save_message(cursor, connection, conversation_id, 'system', f"Chuyển tư vấn viên: {reason}.")
    create_agent_notification(cursor, connection, conversation_id, reason)
    notifier.notify_staff(f"Phiên chat #{conversation_id} cần tư vấn viên",
                          f"Lý do: {reason}.")
    return HandoverResult(LIVE, AGENT_REQUESTED_MESSAGE)


# ---- Ticket hỗ trợ (ngoài giờ) -----------------------------------------------------------------
def validate_ticket(data: dict) -> tuple[dict | None, str | None]:
    """Kiểm tra form để lại thông tin. Trả về (dữ liệu đã chuẩn hoá, None) hoặc
    (None, thông báo lỗi). URD mục 5: chỉ thu thập tên/email/SĐT khi người dùng
    ĐỒNG Ý -> bắt buộc consent=true."""
    if data.get("consent") is not True:
        return None, "Bạn cần đồng ý cho phép lưu thông tin liên hệ để tư vấn viên liên hệ lại."
    full_name = str(data.get("full_name") or "").strip()
    email = str(data.get("email") or "").strip()
    phone = re.sub(r"[\s.\-]", "", str(data.get("phone") or ""))
    content = str(data.get("content") or "").strip()

    if not full_name or len(full_name) > MAX_NAME_CHARS:
        return None, f"Vui lòng nhập họ tên (tối đa {MAX_NAME_CHARS} ký tự)."
    if not email and not phone:
        return None, "Vui lòng nhập email hoặc số điện thoại để tư vấn viên liên hệ lại."
    if email and (len(email) > 255 or not EMAIL_PATTERN.match(email)):
        return None, "Email không hợp lệ."
    if phone and not PHONE_PATTERN.match(phone):
        return None, "Số điện thoại không hợp lệ (vd 0912345678 hoặc +84912345678)."
    if not content or len(content) > MAX_CONTENT_CHARS:
        return None, f"Vui lòng nhập nội dung cần hỗ trợ (tối đa {MAX_CONTENT_CHARS} ký tự)."
    return {"full_name": full_name, "email": email or None, "phone": phone or None,
            "content": content}, None


def open_ticket(cursor, connection, conversation_id: int, user_id: int, ticket: dict) -> int:
    ticket_id = create_support_ticket(cursor, connection, conversation_id=conversation_id,
                                      user_id=user_id, **ticket)
    save_message(cursor, connection, conversation_id, 'system',
                 f"Thí sinh đã để lại thông tin hỗ trợ (ticket #{ticket_id}).")
    create_agent_notification(cursor, connection, conversation_id,
                              f"Ticket hỗ trợ #{ticket_id} (ngoài giờ)")
    notifier.notify_staff(f"Ticket hỗ trợ mới #{ticket_id}",
                          f"Thí sinh để lại yêu cầu hỗ trợ từ phiên chat #{conversation_id}.")
    return ticket_id
