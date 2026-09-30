"""
Thông báo email cho nhân viên trực (D1-09: "Gửi thông báo tức thì In-app/Email").

- In-app: bảng agent_notifications, Staff Dashboard tự hỏi định kỳ.
- Email: gửi qua SMTP ở LUỒNG NỀN để request của thí sinh không phải chờ máy
  chủ mail. Chưa cấu hình SMTP_HOST / STAFF_NOTIFY_EMAILS -> bỏ qua (chỉ log).

Nội dung email cố ý KHÔNG chứa tin nhắn hay thông tin liên hệ của thí sinh
(hạn chế PII đi qua hộp thư); nhân viên mở Staff Dashboard để xem chi tiết.
"""
import logging
import smtplib
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

import config

logger = logging.getLogger("pmaster.notifier")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")


def email_enabled() -> bool:
    return bool(config.SMTP_HOST and config.STAFF_NOTIFY_EMAILS and config.SMTP_FROM)


def _send_email(subject: str, body: str):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_FROM
    message["To"] = ", ".join(config.STAFF_NOTIFY_EMAILS)
    message.set_content(body)
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as smtp:
            if config.SMTP_USE_TLS:
                smtp.starttls()
            if config.SMTP_USER:
                smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.send_message(message)
        logger.info("[Notifier] Đã gửi email thông báo: %s", subject)
    except (OSError, smtplib.SMTPException) as exc:
        logger.error("[Notifier] Gửi email thất bại (%s): %s", type(exc).__name__, exc)


def notify_staff(subject: str, detail: str):
    """Gửi email thông báo (không chặn luồng gọi). Trả về True nếu đã xếp hàng gửi."""
    if not email_enabled():
        logger.info("[Notifier] Email chưa cấu hình, chỉ thông báo trong Dashboard: %s", subject)
        return False
    body = (
        f"{detail}\n\n"
        f"Mở Staff Dashboard để xem lịch sử và tiếp nhận: {config.STAFF_DASHBOARD_URL}\n\n"
        "-- Trợ lý AI Python Master 2026 (email tự động, vui lòng không trả lời)"
    )
    _executor.submit(_send_email, f"[Python Master] {subject}", body)
    return True
