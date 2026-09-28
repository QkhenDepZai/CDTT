-- =====================================================================
-- (Khuyến nghị cho môi trường thật) Tạo tài khoản MySQL RIÊNG cho ứng dụng
-- với quyền tối thiểu, thay vì để app chạy bằng root.
--
-- Chạy bằng root 1 lần:  mysql -u root -p < sql/03_create_app_user.sql
-- TRƯỚC KHI CHẠY: đổi mật khẩu bên dưới thành chuỗi mạnh (>= 16 ký tự),
-- rồi đặt cùng giá trị đó vào DB_USER / DB_PASSWORD trong file .env.
-- Không commit mật khẩu thật lên Git.
-- =====================================================================

CREATE USER IF NOT EXISTS 'pmaster_app'@'localhost'
    IDENTIFIED BY 'Doi_Mat_Khau_Manh_Truoc_Khi_Chay_2026!';

-- Chỉ DML trên đúng database của ứng dụng; KHÔNG cấp DROP/ALTER/GRANT.
-- (import_faqs.py dùng TRUNCATE -> cần chạy script đó bằng tài khoản quản trị.)
GRANT SELECT, INSERT, UPDATE, DELETE
    ON gemini_chat_db.* TO 'pmaster_app'@'localhost';

FLUSH PRIVILEGES;
