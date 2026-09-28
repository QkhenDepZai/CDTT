"""
Cấu hình chung cho pytest.

- Unit test (chunker, loader, vector index) chạy không cần MySQL/API key.
- Integration test (test_ingest_integration.py) chỉ chạy khi đặt biến môi
  trường PM_TEST_MYSQL=1; dùng database RIÊNG (mặc định gemini_chat_db_test)
  để không đụng dữ liệu thật, và embedding giả lập (không gọi Gemini).
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Phải đặt TRƯỚC khi config.py được import lần đầu (config đọc env lúc import).
if os.getenv("PM_TEST_MYSQL") == "1":
    os.environ["DB_NAME"] = os.getenv("PM_TEST_DB_NAME", "gemini_chat_db_test")
