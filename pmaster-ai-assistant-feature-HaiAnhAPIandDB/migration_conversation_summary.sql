USE gemini_chat_db;

-- Migration bổ sung cơ chế "tóm tắt hội thoại" để giảm token khi gửi history
-- cho Gemini (xem conversation_memory.py + AUDIT_REPORT.md mục 5).
-- Chạy sau migration_agent_support.sql. An toàn để chạy lại nhiều lần nhờ
-- IF NOT EXISTS / kiểm tra cột trước khi thêm tùy theo phiên bản MySQL.

ALTER TABLE conversations
    ADD COLUMN history_summary TEXT NULL,
    ADD COLUMN summary_covers_up_to_id INT NOT NULL DEFAULT 0;
