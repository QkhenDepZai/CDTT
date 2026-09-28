-- =====================================================================
-- MIGRATION v1 -> v2 (Knowledge Base RAG + đa kênh) - MySQL 8.0+
--
-- Dùng khi database gemini_chat_db ĐÃ được tạo từ DB_PythonMaster.sql cũ
-- (có hoặc chưa chạy migration_conversation_summary.sql).
-- Chạy:  mysql -u root -p < sql/02_migration_v1_to_v2.sql
--
-- Idempotent: MySQL 8.0 không hỗ trợ "ADD COLUMN IF NOT EXISTS", nên dùng
-- stored procedure tạm kiểm tra information_schema trước khi ALTER.
-- NÊN backup trước khi chạy:  mysqldump -u root -p gemini_chat_db > backup.sql
-- =====================================================================

USE gemini_chat_db;

DROP PROCEDURE IF EXISTS pm_add_column;
DROP PROCEDURE IF EXISTS pm_add_index;

DELIMITER $$

CREATE PROCEDURE pm_add_column(
    IN p_table VARCHAR(64), IN p_column VARCHAR(64), IN p_definition TEXT)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = p_table AND COLUMN_NAME = p_column
    ) THEN
        SET @ddl = CONCAT('ALTER TABLE `', p_table, '` ADD COLUMN `',
                          p_column, '` ', p_definition);
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$

CREATE PROCEDURE pm_add_index(
    IN p_table VARCHAR(64), IN p_index VARCHAR(64), IN p_definition TEXT)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = p_table AND INDEX_NAME = p_index
    ) THEN
        SET @ddl = CONCAT('ALTER TABLE `', p_table, '` ADD ', p_definition);
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$

DELIMITER ;

-- ---- users ----------------------------------------------------------
ALTER TABLE users
    MODIFY COLUMN role ENUM('user', 'dev', 'staff', 'admin') NOT NULL DEFAULT 'user';
CALL pm_add_column('users', 'display_name', 'VARCHAR(100) NULL AFTER username');
CALL pm_add_column('users', 'email', 'VARCHAR(255) NULL AFTER display_name');
CALL pm_add_column('users', 'phone', 'VARCHAR(20) NULL AFTER email');
CALL pm_add_column('users', 'pii_consent_at', 'TIMESTAMP NULL AFTER phone');
CALL pm_add_column('users', 'is_active', 'TINYINT(1) NOT NULL DEFAULT 1 AFTER role');
CALL pm_add_column('users', 'last_seen_at', 'TIMESTAMP NULL AFTER is_active');
CALL pm_add_column('users', 'updated_at',
    'TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP');
CALL pm_add_index('users', 'idx_users_role', 'KEY idx_users_role (role)');
CALL pm_add_index('users', 'idx_users_email', 'KEY idx_users_email (email)');

-- ---- conversations --------------------------------------------------
CALL pm_add_column('conversations', 'channel',
    "ENUM('web', 'messenger', 'zalo') NOT NULL DEFAULT 'web' AFTER site_id");
CALL pm_add_column('conversations', 'history_summary', 'TEXT NULL');
CALL pm_add_column('conversations', 'summary_covers_up_to_id', 'INT NOT NULL DEFAULT 0');
CALL pm_add_column('conversations', 'last_message_at', 'TIMESTAMP NULL');
CALL pm_add_index('conversations', 'idx_conv_user_created',
    'KEY idx_conv_user_created (user_id, created_at)');
CALL pm_add_index('conversations', 'idx_conv_status',
    'KEY idx_conv_status (status, created_at)');

-- ---- messages -------------------------------------------------------
CALL pm_add_column('messages', 'answer_status', 'VARCHAR(32) NULL AFTER image_url');
CALL pm_add_column('messages', 'retrieved_chunk_ids', 'JSON NULL AFTER answer_status');
CALL pm_add_column('messages', 'latency_ms', 'INT NULL AFTER retrieved_chunk_ids');
CALL pm_add_index('messages', 'idx_messages_conv',
    'KEY idx_messages_conv (conversation_id, id)');
CALL pm_add_index('messages', 'idx_messages_status',
    'KEY idx_messages_status (answer_status, created_at)');

-- ---- bảng mới -------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_channel_identities (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    user_id            INT          NOT NULL,
    channel            ENUM('web', 'messenger', 'zalo') NOT NULL,
    channel_account_id VARCHAR(128) NOT NULL DEFAULT '',
    external_user_id   VARCHAR(128) NOT NULL,
    created_at         TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_channel_identity (channel, channel_account_id, external_user_id),
    KEY idx_identity_user (user_id),
    CONSTRAINT fk_identity_users FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knowledge_metadata (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    title              VARCHAR(255) NOT NULL,
    source_type        ENUM('pdf', 'txt', 'csv', 'docx', 'md', 'faq') NOT NULL,
    original_filename  VARCHAR(255) NULL,
    storage_path       VARCHAR(500) NULL,
    mime_type          VARCHAR(100) NULL,
    file_size_bytes    BIGINT       NULL,
    content_hash       CHAR(64)     NOT NULL,
    category           VARCHAR(100) NULL,
    language           VARCHAR(10)  NOT NULL DEFAULT 'vi',
    status             ENUM('pending', 'processing', 'ready', 'failed')
                                    NOT NULL DEFAULT 'pending',
    error_message      VARCHAR(1000) NULL,
    chunk_count        INT          NOT NULL DEFAULT 0,
    embedding_model    VARCHAR(100) NULL,
    embedding_dim      SMALLINT     NULL,
    is_active          TINYINT(1)   NOT NULL DEFAULT 1,
    uploaded_by        INT          NULL,
    processed_at       TIMESTAMP    NULL,
    created_at         TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_knowledge_hash (content_hash),
    KEY idx_knowledge_status (status, is_active),
    KEY idx_knowledge_category (category),
    CONSTRAINT fk_knowledge_uploader FOREIGN KEY (uploaded_by)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    document_id      INT          NOT NULL,
    chunk_index      INT          NOT NULL,
    content          MEDIUMTEXT   NOT NULL,
    content_hash     CHAR(64)     NOT NULL,
    char_count       INT          NOT NULL,
    token_estimate   INT          NOT NULL,
    page_number      INT          NULL,
    metadata         JSON         NULL,
    embedding        BLOB         NULL,
    embedding_model  VARCHAR(100) NULL,
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_chunk_position (document_id, chunk_index),
    KEY idx_chunk_hash (content_hash),
    FULLTEXT KEY ft_chunk_content (content) WITH PARSER ngram,
    CONSTRAINT fk_chunks_document FOREIGN KEY (document_id)
        REFERENCES knowledge_metadata(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- violation_logs / agent_notifications: tạo nếu DB rất cũ chưa có.
CREATE TABLE IF NOT EXISTS violation_logs (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    conversation_id  INT          NOT NULL,
    user_id          INT          NULL,
    content          TEXT         NOT NULL,
    reason           VARCHAR(255) NULL,
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_violation_conversations FOREIGN KEY (conversation_id)
        REFERENCES conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_violation_users FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_notifications (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    conversation_id  INT          NOT NULL,
    reason           VARCHAR(255) NOT NULL,
    is_read          TINYINT(1)   NOT NULL DEFAULT 0,
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at          TIMESTAMP    NULL,
    CONSTRAINT fk_agent_notifications_conversations FOREIGN KEY (conversation_id)
        REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE OR REPLACE VIEW chat_history AS
SELECT
    m.id               AS message_id,
    c.user_id          AS user_id,
    m.conversation_id  AS conversation_id,
    c.channel          AS channel,
    m.sender_type      AS sender_type,
    m.content          AS content,
    m.image_url        AS image_url,
    m.answer_status    AS answer_status,
    m.retrieved_chunk_ids AS retrieved_chunk_ids,
    m.latency_ms       AS latency_ms,
    m.created_at       AS created_at
FROM messages m
JOIN conversations c ON c.id = m.conversation_id;

-- ---------------------------------------------------------------------
-- 6. ĐA KÊNH (Giai đoạn 4 - thư mục channels/)
--    channel_inbound_events: chống xử lý trùng. Facebook/Zalo gửi lại
--      (retry) webhook nếu không nhận 200 kịp -> UNIQUE(channel, event_key)
--      đảm bảo 1 tin nhắn chỉ được trả lời 1 lần, kể cả khi chạy nhiều worker.
--    channel_tokens: access/refresh token của Zalo OA. Zalo cấp refresh
--      token MỚI sau mỗi lần làm mới (token cũ mất hiệu lực) nên phải lưu bền
--      vững trong DB, không thể chỉ để trong .env.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_inbound_events (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    channel      ENUM('web', 'messenger', 'zalo') NOT NULL,
    event_key    VARCHAR(191) NOT NULL,
    received_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_inbound_event (channel, event_key),
    KEY idx_inbound_received (received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS channel_tokens (
    channel             ENUM('messenger', 'zalo') NOT NULL,
    channel_account_id  VARCHAR(128) NOT NULL,
    access_token        TEXT         NOT NULL,
    refresh_token       TEXT         NULL,
    expires_at          DATETIME     NULL,
    updated_at          TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (channel, channel_account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DROP PROCEDURE IF EXISTS pm_add_column;
DROP PROCEDURE IF EXISTS pm_add_index;
