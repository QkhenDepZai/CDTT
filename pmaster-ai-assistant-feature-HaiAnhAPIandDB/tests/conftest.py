"""
Cấu hình chung cho pytest.

- Unit test (chunker, loader, vector index) chạy không cần MySQL/API key.
- Integration test (test_ingest_integration.py) chỉ chạy khi đặt biến môi
  trường PM_TEST_MYSQL=1; dùng database RIÊNG (mặc định gemini_chat_db_test)
  để không đụng dữ liệu thật, và embedding giả lập (không gọi Gemini).
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# gemini_client khởi tạo genai.Client ngay khi import (bắt buộc có key).
# Key giả chỉ để import được; mọi lệnh gọi Gemini trong test đều bị thay bằng
# bản giả lập, không có request nào đi ra Internet.
os.environ.setdefault("GEMINI_API_KEY", "test-key-khong-dung-that")

# Phải đặt TRƯỚC khi config.py được import lần đầu (config đọc env lúc import).
if os.getenv("PM_TEST_MYSQL") == "1":
    os.environ["DB_NAME"] = os.getenv("PM_TEST_DB_NAME", "gemini_chat_db_test")


SCHEMA_PATH = os.path.join(PROJECT_ROOT, "sql", "01_init_schema.sql")


@pytest.fixture(scope="module")
def test_db():
    import pymysql
    from config import DB_CONFIG

    db_name = DB_CONFIG["database"]
    assert db_name.endswith("_test"), "Chỉ chạy integration test trên database *_test"

    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        script = handle.read().replace("gemini_chat_db", db_name)
    script = "\n".join(line for line in script.splitlines() if not line.strip().startswith("--"))

    admin = pymysql.connect(host=DB_CONFIG["host"], user=DB_CONFIG["user"],
                            password=DB_CONFIG["password"], autocommit=True)
    with admin.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        for statement in script.split(";"):
            if statement.strip():
                cursor.execute(statement)
    yield db_name
    with admin.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    admin.close()
