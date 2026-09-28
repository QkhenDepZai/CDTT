USE gemini_chat_db;

-- Migration cho database đã tồn tại từ phiên bản cũ.
-- Chạy lần lượt và kiểm tra schema trước/sau khi áp dụng.

ALTER TABLE users
    MODIFY COLUMN role ENUM('user', 'dev', 'staff') DEFAULT 'user';

ALTER TABLE conversations
    ADD COLUMN status ENUM('bot', 'waiting_agent', 'agent', 'closed') DEFAULT 'bot',
    ADD COLUMN fail_count INT DEFAULT 0,
    ADD COLUMN assigned_staff_id INT NULL,
    ADD COLUMN closed_at TIMESTAMP NULL;

ALTER TABLE conversations
    ADD CONSTRAINT fk_conversations_staff
    FOREIGN KEY (assigned_staff_id) REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE messages
    MODIFY COLUMN sender_type ENUM('user', 'bot', 'model', 'staff', 'system') NOT NULL,
    ADD COLUMN image_url VARCHAR(500) NULL;

CREATE TABLE IF NOT EXISTS violation_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    conversation_id INT NOT NULL,
    user_id INT NULL,
    content TEXT NOT NULL,
    reason VARCHAR(255) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_notifications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    conversation_id INT NOT NULL,
    reason VARCHAR(255) NOT NULL,
    is_read TINYINT(1) DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    read_at TIMESTAMP NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
