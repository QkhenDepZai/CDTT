"""
Điểm chạy chính của Chatbot AI Python Master 2026.

PyCharm: chuột phải main.py -> Run 'main' (Working directory = thư mục dự án,
để .env và uploads/ được tìm đúng chỗ).
Dòng lệnh: python main.py

Trình tự khởi động (fail-fast, báo lỗi rõ ràng thay vì lỗi mơ hồ lúc chat):
1. Kiểm tra cấu hình bắt buộc (.env): GEMINI_API_KEY, DB_PASSWORD...
2. Kiểm tra kết nối MySQL.
3. Nạp sẵn chỉ mục vector Knowledge Base vào RAM (warm-up) -> câu hỏi
   đầu tiên không phải chờ nạp chỉ mục.
4. Chạy Flask server.

Production: KHÔNG dùng server dev của Flask, chạy bằng WSGI server, ví dụ
    waitress-serve --port=5000 app:app        (Windows)
    gunicorn -w 4 -b 0.0.0.0:5000 app:app     (Linux)
Mỗi worker tự nạp chỉ mục KB và tự làm mới khi KB thay đổi
(RAG_INDEX_REFRESH_SECONDS).
"""
import logging
import sys

from config import (
    ADMIN_API_KEY,
    APP_HOST,
    APP_PORT,
    APP_SECRET_KEY,
    DB_CONFIG,
    ENFORCE_USER_TOKEN,
    GEMINI_API_KEY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("pmaster.main")


def check_config() -> list[str]:
    """Trả về danh sách lỗi cấu hình nghiêm trọng (rỗng = hợp lệ)."""
    problems = []
    if not GEMINI_API_KEY:
        problems.append("Thiếu GEMINI_API_KEY trong .env (lấy tại https://aistudio.google.com/apikey).")
    if DB_CONFIG.get("password") is None:
        problems.append("Thiếu DB_PASSWORD trong .env (để trống giá trị nếu MySQL không đặt mật khẩu).")
    return problems


def warn_optional_config():
    """Thiếu các khoá này KHÔNG chặn chatbot, nhưng tắt tính năng liên quan."""
    if len(APP_SECRET_KEY) < 32:
        logger.warning("APP_SECRET_KEY chưa đặt hoặc < 32 ký tự: không cấp được user_token -> "
                       "widget không khôi phục phiên, không nhận tin tư vấn viên; "
                       "API /api/history/* sẽ từ chối mọi yêu cầu.")
        if ENFORCE_USER_TOKEN:
            logger.warning("ENFORCE_USER_TOKEN=true nhưng thiếu APP_SECRET_KEY: người dùng cũ "
                           "sẽ không chat tiếp được trong phiên có sẵn.")
    if len(ADMIN_API_KEY) < 16:
        logger.warning("ADMIN_API_KEY chưa đặt hoặc < 16 ký tự: API quản trị /api/knowledge/* "
                       "đang bị khoá (chỉ quản trị được qua manage_knowledge.py).")

    import handover
    logger.info("Giờ trực tư vấn viên: %s.", handover.support_hours_label())

    from channels.registry import channel_status
    for channel, enabled in channel_status().items():
        if enabled:
            logger.info("Kênh %s: BẬT (webhook /webhooks/%s).", channel, channel)
        else:
            logger.info("Kênh %s: tắt (chưa điền đủ khoá trong .env).", channel)


def warm_up_knowledge_base():
    """Nạp chỉ mục vector. Lỗi ở đây KHÔNG chặn khởi động: chatbot vẫn chạy
    với FAQ + FULLTEXT dự phòng, và chỉ mục sẽ tự nạp lại ở câu hỏi sau."""
    try:
        from knowledge.retriever import get_retriever
        count = get_retriever().reload()
        if count == 0:
            logger.warning(
                "Knowledge Base đang trống. Chạy: python manage_knowledge.py sync-faq "
                "và python manage_knowledge.py ingest <file>"
            )
        else:
            logger.info("Knowledge Base sẵn sàng: %s đoạn tri thức.", count)
    except Exception as exc:  # noqa: BLE001
        logger.error("Không nạp được chỉ mục Knowledge Base (%s). "
                     "Đã chạy sql/01_init_schema.sql hoặc 02_migration chưa?", exc)


def main() -> int:
    problems = check_config()
    if problems:
        for problem in problems:
            logger.error("Cấu hình: %s", problem)
        logger.error("Sao chép .env.example thành .env và điền đủ giá trị rồi chạy lại.")
        return 1
    warn_optional_config()

    from database import ping_database
    db_ok, db_error = ping_database()
    if not db_ok:
        logger.error("Không kết nối được MySQL %s@%s/%s: %s",
                     DB_CONFIG.get("user"), DB_CONFIG.get("host"),
                     DB_CONFIG.get("database"), db_error)
        return 1
    logger.info("Kết nối MySQL OK (database=%s).", DB_CONFIG.get("database"))

    warm_up_knowledge_base()

    # Import app SAU khi kiểm tra cấu hình: gemini_client khởi tạo Gemini
    # client ngay lúc import và sẽ lỗi khó hiểu nếu thiếu API key.
    from app import app

    logger.info("Server chạy tại http://%s:%s (health: /api/health)", APP_HOST, APP_PORT)
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
