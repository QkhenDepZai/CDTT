"""SQL cho đa kênh: chống trùng sự kiện, ánh xạ người dùng, phiên chat, token."""
from __future__ import annotations

import pymysql

# Giữ lịch sử event_key đủ lâu để chặn mọi lần gửi lại của nền tảng.
INBOUND_EVENT_RETENTION_DAYS = 7


def record_inbound_event(cursor, connection, channel: str, event_key: str) -> bool:
    """True nếu là sự kiện MỚI; False nếu đã nhận trước đó (webhook gửi lại)."""
    cursor.execute(
        "INSERT IGNORE INTO channel_inbound_events (channel, event_key) VALUES (%s, %s)",
        (channel, event_key[:191]),
    )
    connection.commit()
    return cursor.rowcount == 1


def purge_old_inbound_events(cursor, connection) -> int:
    cursor.execute(
        "DELETE FROM channel_inbound_events "
        "WHERE received_at < NOW() - INTERVAL %s DAY LIMIT 5000",
        (INBOUND_EVENT_RETENTION_DAYS,),
    )
    connection.commit()
    return cursor.rowcount


def get_or_create_channel_user(cursor, connection, channel: str, account_id: str,
                               external_user_id: str) -> tuple[int, bool]:
    """Trả về (users.id, is_new). An toàn khi nhiều worker cùng tạo 1 người
    dùng: UNIQUE key của user_channel_identities quyết định ai thắng."""
    lookup = (
        "SELECT user_id FROM user_channel_identities "
        "WHERE channel = %s AND channel_account_id = %s AND external_user_id = %s"
    )
    params = (channel, account_id, external_user_id)
    cursor.execute(lookup, params)
    row = cursor.fetchone()
    if row:
        return row["user_id"], False

    try:
        connection.begin()
        cursor.execute(
            "INSERT INTO users (username, role) VALUES (%s, 'user')",
            (f"{channel}_{external_user_id}"[:50],),
        )
        user_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO user_channel_identities "
            "(user_id, channel, channel_account_id, external_user_id) VALUES (%s, %s, %s, %s)",
            (user_id, channel, account_id, external_user_id),
        )
        connection.commit()
        return user_id, True
    except pymysql.err.IntegrityError:
        connection.rollback()
        cursor.execute(lookup, params)
        return cursor.fetchone()["user_id"], False


def get_active_channel_conversation(cursor, connection, user_id: int, channel: str) -> dict:
    """Phiên gần nhất chưa đóng của người dùng trên kênh; nếu phiên gần nhất
    đã đóng (tư vấn viên kết thúc) hoặc chưa có -> mở phiên mới."""
    columns = "id, user_id, status, fail_count, assigned_staff_id, site_id"
    cursor.execute(
        f"SELECT {columns} FROM conversations WHERE user_id = %s AND channel = %s "
        "ORDER BY id DESC LIMIT 1",
        (user_id, channel),
    )
    row = cursor.fetchone()
    if row and row["status"] != "closed":
        return row

    title = {"messenger": "Facebook Messenger", "zalo": "Zalo OA"}.get(channel, channel)
    cursor.execute(
        "INSERT INTO conversations (user_id, channel, title, status, fail_count) "
        "VALUES (%s, %s, %s, 'bot', 0)",
        (user_id, channel, f"Chat qua {title}"),
    )
    connection.commit()
    cursor.execute(f"SELECT {columns} FROM conversations WHERE id = %s", (cursor.lastrowid,))
    return cursor.fetchone()


def get_channel_recipient(cursor, conversation_id: int) -> dict | None:
    """Kênh + ID người nhận của 1 phiên (dùng để đẩy tin tư vấn viên ra ngoài).
    None nếu phiên thuộc kênh web (web tự lấy tin qua API)."""
    cursor.execute(
        "SELECT c.channel, i.channel_account_id, i.external_user_id "
        "FROM conversations c "
        "JOIN user_channel_identities i ON i.user_id = c.user_id AND i.channel = c.channel "
        "WHERE c.id = %s AND c.channel IN ('messenger', 'zalo') "
        "ORDER BY i.id DESC LIMIT 1",
        (conversation_id,),
    )
    return cursor.fetchone()


def get_token(cursor, channel: str, account_id: str) -> dict | None:
    cursor.execute(
        "SELECT access_token, refresh_token, expires_at, "
        "expires_at IS NOT NULL AND expires_at <= NOW() + INTERVAL 5 MINUTE AS expiring "
        "FROM channel_tokens WHERE channel = %s AND channel_account_id = %s",
        (channel, account_id),
    )
    return cursor.fetchone()


def save_token(cursor, connection, channel: str, account_id: str, access_token: str,
               refresh_token: str | None, expires_in_seconds: int | None):
    cursor.execute(
        "INSERT INTO channel_tokens (channel, channel_account_id, access_token, refresh_token, "
        "expires_at) VALUES (%s, %s, %s, %s, "
        "IF(%s IS NULL, NULL, NOW() + INTERVAL %s SECOND)) "
        "ON DUPLICATE KEY UPDATE access_token = VALUES(access_token), "
        "refresh_token = COALESCE(VALUES(refresh_token), refresh_token), "
        "expires_at = VALUES(expires_at)",
        (channel, account_id, access_token, refresh_token, expires_in_seconds,
         expires_in_seconds or 0),
    )
    connection.commit()
