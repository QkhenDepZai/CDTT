import json
import logging

import pymysql
from config import DB_CONFIG

logger = logging.getLogger("pmaster.database")

FAIL_COUNT_ESCALATE_THRESHOLD = 3

# MySQL error 1054 "Unknown column" - dùng để nhận biết DB chưa chạy migration v2.
ER_BAD_FIELD_ERROR = 1054
_messages_has_rag_columns = True


def get_db_connection():
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB_CONFIG)


def get_site_by_key(site_key):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, allowed_domain, is_active FROM sites WHERE site_key = %s",
                (site_key,)
            )
            return cursor.fetchone()
    finally:
        connection.close()


def get_user_role(cursor, user_id):
    cursor.execute("SELECT role FROM users WHERE id = %s", (user_id,))
    row = cursor.fetchone()
    return row["role"] if row else None


def get_or_create_user(cursor, connection, user_id):
    if user_id:
        cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
        if cursor.fetchone():
            return user_id
    cursor.execute("INSERT INTO users (username) VALUES (%s)", ("Thi sinh moi",))
    connection.commit()
    return cursor.lastrowid


def create_conversation(cursor, connection, user_id, title, site_id):
    cursor.execute(
        "INSERT INTO conversations (user_id, title, site_id, status, fail_count) "
        "VALUES (%s, %s, %s, 'bot', 0)",
        (user_id, title, site_id)
    )
    connection.commit()
    return cursor.lastrowid


def get_or_create_conversation(cursor, connection, conversation_id, user_id, title, site_id):
    if conversation_id:
        cursor.execute(
            "SELECT id, user_id, status, fail_count, assigned_staff_id, site_id "
            "FROM conversations WHERE id = %s AND user_id = %s",
            (conversation_id, user_id)
        )
        row = cursor.fetchone()
        if row:
            return row
    new_id = create_conversation(cursor, connection, user_id, title, site_id)
    return {"id": new_id, "user_id": user_id, "status": "bot", "fail_count": 0,
            "assigned_staff_id": None, "site_id": site_id}


def get_conversation(cursor, conversation_id):
    cursor.execute(
        "SELECT id, user_id, site_id, status, fail_count, assigned_staff_id, created_at, closed_at "
        "FROM conversations WHERE id = %s",
        (conversation_id,)
    )
    return cursor.fetchone()


def list_user_conversations(cursor, user_id, limit=50):
    cursor.execute(
        "SELECT id, title, status, fail_count, created_at, closed_at "
        "FROM conversations WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
        (user_id, limit)
    )
    return cursor.fetchall()


def set_conversation_status(cursor, connection, conversation_id, status):
    cursor.execute("UPDATE conversations SET status = %s WHERE id = %s", (status, conversation_id))
    connection.commit()


def increment_fail_count(cursor, connection, conversation_id):
    cursor.execute(
        "UPDATE conversations SET fail_count = fail_count + 1 WHERE id = %s",
        (conversation_id,)
    )
    connection.commit()
    cursor.execute("SELECT fail_count FROM conversations WHERE id = %s", (conversation_id,))
    fail_count = cursor.fetchone()["fail_count"]
    if fail_count >= FAIL_COUNT_ESCALATE_THRESHOLD:
        set_conversation_status(cursor, connection, conversation_id, "waiting_agent")
    return fail_count


def reset_fail_count(cursor, connection, conversation_id):
    cursor.execute("UPDATE conversations SET fail_count = 0 WHERE id = %s", (conversation_id,))
    connection.commit()


def assign_staff(cursor, connection, conversation_id, staff_id):
    cursor.execute(
        "UPDATE conversations SET status = 'agent', assigned_staff_id = %s WHERE id = %s",
        (staff_id, conversation_id)
    )
    connection.commit()


def close_conversation(cursor, connection, conversation_id):
    cursor.execute(
        "UPDATE conversations SET status = 'closed', closed_at = NOW() WHERE id = %s",
        (conversation_id,)
    )
    connection.commit()


def list_waiting_conversations(cursor):
    cursor.execute(
        "SELECT c.id, c.user_id, c.title, c.fail_count, c.created_at, u.username "
        "FROM conversations c JOIN users u ON u.id = c.user_id "
        "WHERE c.status = 'waiting_agent' ORDER BY c.created_at ASC"
    )
    return cursor.fetchall()


def save_message(cursor, connection, conversation_id, sender_type, content, image_url=None,
                 answer_status=None, retrieved_chunk_ids=None, latency_ms=None):
    """Lưu 1 tin nhắn. 3 tham số cuối (cột thêm ở sql/02_migration_v1_to_v2.sql)
    dùng để truy vết RAG/hallucination (D1-10) và thống kê (D1-12)."""
    global _messages_has_rag_columns
    if _messages_has_rag_columns and (
        answer_status is not None or retrieved_chunk_ids is not None or latency_ms is not None
    ):
        try:
            cursor.execute(
                "INSERT INTO messages (conversation_id, sender_type, content, image_url, "
                "answer_status, retrieved_chunk_ids, latency_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (conversation_id, sender_type, content or "", image_url, answer_status,
                 json.dumps(retrieved_chunk_ids) if retrieved_chunk_ids is not None else None,
                 latency_ms)
            )
            connection.commit()
            return
        except pymysql.err.OperationalError as exc:
            if exc.args and exc.args[0] == ER_BAD_FIELD_ERROR:
                # DB chưa chạy migration v2 -> tắt ghi cột mới, không làm hỏng luồng chat.
                _messages_has_rag_columns = False
                logger.warning("Bảng messages chưa có cột RAG, hãy chạy "
                               "sql/02_migration_v1_to_v2.sql. Tạm lưu tin nhắn dạng cũ.")
            else:
                raise
    cursor.execute(
        "INSERT INTO messages (conversation_id, sender_type, content, image_url) "
        "VALUES (%s, %s, %s, %s)",
        (conversation_id, sender_type, content or "", image_url)
    )
    connection.commit()


def get_conversation_messages(cursor, conversation_id):
    cursor.execute(
        "SELECT id, sender_type, content, image_url, created_at "
        "FROM messages WHERE conversation_id = %s ORDER BY created_at ASC",
        (conversation_id,)
    )
    return cursor.fetchall()


# ============================================================
# TÓM TẮT HỘI THOẠI (giảm token khi conversation dài - xem conversation_memory.py)
# Yêu cầu chạy migration_conversation_summary.sql trước khi dùng 2 hàm dưới đây.
# ============================================================
def get_conversation_summary_state(cursor, conversation_id):
    """Trả về {'history_summary': str|None, 'summary_covers_up_to_id': int}.
    summary_covers_up_to_id = id message cuối cùng đã được gộp vào summary,
    dùng để biết những message nào là "cũ, chưa tóm tắt" ở lần tiếp theo."""
    cursor.execute(
        "SELECT history_summary, summary_covers_up_to_id "
        "FROM conversations WHERE id = %s",
        (conversation_id,)
    )
    row = cursor.fetchone() or {}
    return {
        "history_summary": row.get("history_summary"),
        "summary_covers_up_to_id": row.get("summary_covers_up_to_id") or 0,
    }


def update_conversation_summary(cursor, connection, conversation_id, summary_text, covers_up_to_id):
    cursor.execute(
        "UPDATE conversations SET history_summary = %s, summary_covers_up_to_id = %s "
        "WHERE id = %s",
        (summary_text, covers_up_to_id, conversation_id)
    )
    connection.commit()


def log_violation(cursor, connection, conversation_id, user_id, content, reason):
    cursor.execute(
        "INSERT INTO violation_logs (conversation_id, user_id, content, reason) "
        "VALUES (%s, %s, %s, %s)",
        (conversation_id, user_id, content, reason)
    )
    connection.commit()


def create_agent_notification(cursor, connection, conversation_id, reason):
    cursor.execute(
        "INSERT INTO agent_notifications (conversation_id, reason) VALUES (%s, %s)",
        (conversation_id, reason)
    )
    connection.commit()


def list_unread_agent_notifications(cursor, limit=50):
    cursor.execute(
        "SELECT id, conversation_id, reason, created_at FROM agent_notifications "
        "WHERE is_read = 0 ORDER BY created_at ASC LIMIT %s",
        (limit,)
    )
    return cursor.fetchall()


def mark_agent_notification_read(cursor, connection, notification_id):
    cursor.execute(
        "UPDATE agent_notifications SET is_read = 1, read_at = NOW() WHERE id = %s",
        (notification_id,)
    )
    connection.commit()


def get_faq_suggestions(cursor, limit=5):
    cursor.execute(
        "SELECT id, intent, cau_hoi_mau FROM faqs WHERE is_active = 1 LIMIT %s",
        (limit,)
    )
    return cursor.fetchall()


def get_faq_by_id(cursor, faq_id):
    cursor.execute(
        "SELECT id, intent, tra_loi_chuan FROM faqs WHERE id = %s AND is_active = 1",
        (faq_id,)
    )
    return cursor.fetchone()


def ping_database():
    """Kiểm tra kết nối MySQL. Trả về (ok: bool, error: str | None)."""
    try:
        connection = get_db_connection()
    except pymysql.MySQLError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return True, None
    except pymysql.MySQLError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()


def list_user_messages(cursor, user_id, conversation_id=None, before_id=None, limit=50):
    """Lịch sử chat của 1 user từ VIEW chat_history (sql/01_init_schema.sql).

    Phân trang kiểu keyset (before_id) thay vì OFFSET: ổn định khi đang có tin
    nhắn mới chèn vào và không chậm dần khi lịch sử dài. Trả về tin nhắn MỚI
    NHẤT trước. Không trả retrieved_chunk_ids (dữ liệu nội bộ).
    """
    conditions = ["user_id = %s"]
    params = [user_id]
    if conversation_id:
        conditions.append("conversation_id = %s")
        params.append(conversation_id)
    if before_id:
        conditions.append("message_id < %s")
        params.append(before_id)
    params.append(limit)
    cursor.execute(
        "SELECT message_id, conversation_id, channel, sender_type, content, image_url, "
        "answer_status, created_at FROM chat_history "
        f"WHERE {' AND '.join(conditions)} ORDER BY message_id DESC LIMIT %s",
        tuple(params),
    )
    return cursor.fetchall()


def get_user_conversation(cursor, user_id, conversation_id):
    cursor.execute(
        "SELECT id, title, channel, status, created_at, closed_at FROM conversations "
        "WHERE id = %s AND user_id = %s",
        (conversation_id, user_id),
    )
    return cursor.fetchone()
