"""
Khởi tạo / nâng cấp database bằng Python - không cần lệnh `mysql` hay dấu `<`
(tránh lỗi PowerShell "The '<' operator is reserved").

    python init_db.py

Dùng thông tin kết nối trong .env (DB_HOST, DB_USER, DB_PASSWORD, DB_NAME).
Tự nhận biết:
- Database CHƯA có      -> chạy sql/01_init_schema.sql
- Database ĐÃ có (cũ)   -> chạy sql/02_migration_v1_to_v2.sql rồi sql/01_init_schema.sql
Cả 2 script đều idempotent: chạy lại nhiều lần không mất dữ liệu.
"""
import logging
import os
import re
import sys

import pymysql

from config import DB_CONFIG

logger = logging.getLogger("pmaster.init_db")

SQL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sql")
DEFAULT_DB_NAME = "gemini_chat_db"
_DELIMITER_LINE = re.compile(r"^\s*DELIMITER\s+(\S+)\s*$", re.IGNORECASE)


def split_sql_script(script):
    """Tách script thành từng câu lệnh, hỗ trợ lệnh DELIMITER (dùng cho
    stored procedure trong 02_migration) và bỏ qua dòng comment '--'."""
    statements, buffer, delimiter = [], [], ";"
    for line in script.splitlines():
        match = _DELIMITER_LINE.match(line)
        if match:
            delimiter = match.group(1)
            continue
        if not buffer and (not line.strip() or line.strip().startswith("--")):
            continue
        buffer.append(line)
        if line.rstrip().endswith(delimiter):
            statement = "\n".join(buffer).rstrip()[: -len(delimiter)].strip()
            if statement:
                statements.append(statement)
            buffer = []
    if "\n".join(buffer).strip():
        statements.append("\n".join(buffer).strip())
    return statements


def run_sql_file(cursor, filename, db_name):
    path = os.path.join(SQL_DIR, filename)
    with open(path, encoding="utf-8") as handle:
        script = handle.read()
    if db_name != DEFAULT_DB_NAME:
        script = script.replace(DEFAULT_DB_NAME, db_name)

    statements = split_sql_script(script)
    for index, statement in enumerate(statements, start=1):
        try:
            cursor.execute(statement)
        except pymysql.MySQLError as exc:
            first_line = statement.splitlines()[0][:100]
            raise RuntimeError(f"{filename}: lỗi ở câu lệnh #{index} ({first_line}...): {exc}") from exc
    print(f"  [OK] {filename}: {len(statements)} câu lệnh")


def database_exists(cursor, db_name):
    cursor.execute("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (db_name,))
    return cursor.fetchone() is not None


def main():
    db_name = DB_CONFIG["database"]
    try:
        connection = pymysql.connect(
            host=DB_CONFIG["host"], user=DB_CONFIG["user"], password=DB_CONFIG["password"] or "",
            charset="utf8mb4", autocommit=True,
        )
    except pymysql.MySQLError as exc:
        print(f"[LỖI] Không kết nối được MySQL {DB_CONFIG['user']}@{DB_CONFIG['host']}: {exc}")
        print("      Kiểm tra MySQL đã chạy chưa và DB_USER / DB_PASSWORD trong .env.")
        return 1

    try:
        with connection.cursor() as cursor:
            if database_exists(cursor, db_name):
                print(f"Database '{db_name}' đã tồn tại -> nâng cấp lên v2...")
                run_sql_file(cursor, "02_migration_v1_to_v2.sql", db_name)
            else:
                print(f"Tạo mới database '{db_name}'...")
            run_sql_file(cursor, "01_init_schema.sql", db_name)

            cursor.execute(f"USE `{db_name}`")
            cursor.execute("SHOW FULL TABLES")
            tables = sorted(row[0] for row in cursor.fetchall())
    except RuntimeError as exc:
        print(f"[LỖI] {exc}")
        return 1
    finally:
        connection.close()

    print(f"\nHoàn tất. Database '{db_name}' có {len(tables)} bảng/view:")
    print("  " + ", ".join(tables))
    print("\nBước tiếp theo:  python import_faqs.py   rồi   python manage_knowledge.py -v sync-faq")
    return 0


if __name__ == "__main__":
    sys.exit(main())
