import json
import logging

import pymysql
from config import DB_CONFIG

logger = logging.getLogger("pmaster.database")

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
    # Việc chuyển tư vấn viên khi đủ ngưỡng do chat_service/handover quyết định
    # (còn phụ thuộc giờ trực), hàm này chỉ đếm.
    return cursor.fetchone()["fail_count"]


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


STAFF_SCOPES = ("waiting", "mine", "open")


def list_waiting_conversations(cursor, scope="waiting", staff_id=None):
    """Danh sách phiên cho Staff Dashboard.
    waiting: đang chờ tư vấn viên | mine: đang do staff_id xử lý | open: cả hai."""
    conditions = {
        "waiting": ("c.status = 'waiting_agent'", ()),
        "mine": ("c.status = 'agent' AND c.assigned_staff_id = %s", (staff_id,)),
        "open": ("(c.status = 'waiting_agent' OR (c.status = 'agent' AND c.assigned_staff_id = %s))",
                 (staff_id,)),
    }
    where, params = conditions[scope]
    cursor.execute(
        "SELECT c.id, c.user_id, c.title, c.status, c.channel, c.fail_count, c.assigned_staff_id, "
        "c.created_at, u.username, "
        "(SELECT MAX(m.id) FROM messages m WHERE m.conversation_id = c.id) AS last_message_id "
        f"FROM conversations c JOIN users u ON u.id = c.user_id WHERE {where} "
        "ORDER BY c.created_at ASC",
        params,
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
        "FROM messages WHERE conversation_id = %s ORDER BY id ASC",
        (conversation_id,)
    )
    return cursor.fetchall()


def get_messages_after(cursor, conversation_id, after_id=0, limit=200):
    """Tin nhắn có id > after_id theo thứ tự thời gian - widget dùng để khôi
    phục phiên và nhận tin tư vấn viên (polling). Không trả dữ liệu truy vết RAG."""
    cursor.execute(
        "SELECT id, sender_type, content, image_url, answer_status, created_at "
        "FROM messages WHERE conversation_id = %s AND id > %s ORDER BY id ASC LIMIT %s",
        (conversation_id, after_id, limit)
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


def get_pending_clarification(cursor, conversation_id):
    """Nếu tin nhắn cuối của bot là câu HỎI LẠI (answer_status='clarify'),
    trả về câu hỏi gốc của người dùng ngay trước đó; ngược lại None.
    Dùng để ghép "điểm số" (câu trả lời) với "Em muốn biết điểm thi" (câu gốc)."""
    cursor.execute(
        "SELECT sender_type, content, answer_status FROM messages "
        "WHERE conversation_id = %s ORDER BY id DESC LIMIT 2",
        (conversation_id,),
    )
    rows = cursor.fetchall()
    if len(rows) == 2 and rows[0]["answer_status"] == "clarify" and rows[1]["sender_type"] == "user":
        return rows[1]["content"]
    return None


# ============================================================
# TICKET HỖ TRỢ NGOÀI GIỜ (handover.py - D1-09)
# ============================================================
TICKET_STATUSES = ("open", "in_progress", "resolved")


def create_support_ticket(cursor, connection, *, conversation_id, user_id, full_name,
                          email, phone, content):
    cursor.execute(
        "INSERT INTO support_tickets (conversation_id, user_id, full_name, email, phone, content) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (conversation_id, user_id, full_name, email, phone, content)
    )
    connection.commit()
    return cursor.lastrowid


def list_support_tickets(cursor, status=None, limit=100):
    where, params = ("WHERE status = %s", (status,)) if status else ("", ())
    cursor.execute(
        "SELECT id, conversation_id, user_id, full_name, email, phone, content, status, "
        "handled_by, created_at, updated_at FROM support_tickets "
        f"{where} ORDER BY id DESC LIMIT %s",
        params + (limit,)
    )
    return cursor.fetchall()


def get_support_ticket(cursor, ticket_id):
    cursor.execute("SELECT id, conversation_id, status FROM support_tickets WHERE id = %s",
                   (ticket_id,))
    return cursor.fetchone()


def update_support_ticket_status(cursor, connection, ticket_id, status, staff_id):
    cursor.execute(
        "UPDATE support_tickets SET status = %s, handled_by = %s WHERE id = %s",
        (status, staff_id, ticket_id)
    )
    connection.commit()


# ============================================================
# QUẢN TRỊ FAQ (routes/admin.py - D1-11)
# ============================================================
FAQ_EDITABLE_FIELDS = ("nhom_nghiep_vu", "intent", "cau_hoi_mau", "tra_loi_chuan")


def list_faqs(cursor, include_inactive=False):
    where = "" if include_inactive else "WHERE is_active = 1"
    cursor.execute(
        "SELECT id, nhom_nghiep_vu, intent, cau_hoi_mau, tra_loi_chuan, is_active, updated_at "
        f"FROM faqs {where} ORDER BY id"
    )
    return cursor.fetchall()


def get_faq(cursor, faq_id):
    cursor.execute(
        "SELECT id, nhom_nghiep_vu, intent, cau_hoi_mau, tra_loi_chuan, is_active, updated_at "
        "FROM faqs WHERE id = %s",
        (faq_id,)
    )
    return cursor.fetchone()


def create_faq(cursor, connection, fields):
    columns = [name for name in FAQ_EDITABLE_FIELDS if name in fields]
    cursor.execute(
        f"INSERT INTO faqs ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
        tuple(fields[name] for name in columns)
    )
    connection.commit()
    return cursor.lastrowid


def update_faq(cursor, connection, faq_id, fields):
    columns = [name for name in (*FAQ_EDITABLE_FIELDS, "is_active") if name in fields]
    cursor.execute(
        f"UPDATE faqs SET {', '.join(f'{name} = %s' for name in columns)} WHERE id = %s",
        (*(fields[name] for name in columns), faq_id)
    )
    connection.commit()


def get_last_message_id(cursor, conversation_id):
    """Id tin nhắn mới nhất của phiên - widget dùng làm mốc khi hỏi tin mới (polling)."""
    cursor.execute("SELECT COALESCE(MAX(id), 0) AS last_id FROM messages WHERE conversation_id = %s",
                   (conversation_id,))
    return cursor.fetchone()["last_id"]
