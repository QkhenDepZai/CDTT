"""
Gán role 'staff' cho 1 user đã có, hoặc tạo mới nếu chưa tồn tại username đó.

Cách dùng (chạy trong thư mục dự án, đã có file .env):
    python create_staff.py <username>

LƯU Ý QUAN TRỌNG:
Bản backend hiện tại CHƯA có hệ thống đăng nhập (password) cho tư vấn viên - các API
/api/staff/* đang tin trực tiếp vào staff_id do client gửi lên (query param ?staff_id=
hoặc header X-Staff-Id), rồi chỉ kiểm tra role của id đó trong bảng users có phải
'staff' hay không (xem security.py -> require_staff_role, routes/staff.py).
Nghĩa là ai biết đúng staff_id là gọi được API tư vấn viên, không cần mật khẩu.
Đây là giới hạn đã ghi trong CHANGELOG_MUST_HAVE.md - CHỈ dùng nội bộ / môi trường dev,
KHÔNG public các route /api/staff/* ra Internet khi chưa nâng cấp thành đăng nhập thật.
"""
import sys

from database import get_db_connection


def main():
    if len(sys.argv) != 2:
        print("Cách dùng: python create_staff.py <username>")
        sys.exit(1)

    username = sys.argv[1].strip()
    if not username or len(username) > 50:
        print("Username không hợp lệ (1-50 ký tự).")
        sys.exit(1)

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, role FROM users WHERE username = %s", (username,))
            row = cursor.fetchone()

            if row:
                if row["role"] == "staff":
                    print(f"User '{username}' (id={row['id']}) đã có role 'staff' sẵn rồi.")
                else:
                    cursor.execute("UPDATE users SET role = 'staff' WHERE id = %s", (row["id"],))
                    print(f"Đã đổi role của '{username}' (id={row['id']}) sang 'staff'.")
                staff_id = row["id"]
            else:
                cursor.execute(
                    "INSERT INTO users (username, role) VALUES (%s, 'staff')", (username,)
                )
                staff_id = cursor.lastrowid
                print(f"Đã tạo tài khoản tư vấn viên '{username}' (id={staff_id}).")

        connection.commit()
        print(
            f"\nDùng staff_id={staff_id} khi gọi các API /api/staff/*:\n"
            f"  - Query param: ?staff_id={staff_id}\n"
            f"  - Hoặc header: X-Staff-Id: {staff_id}"
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
