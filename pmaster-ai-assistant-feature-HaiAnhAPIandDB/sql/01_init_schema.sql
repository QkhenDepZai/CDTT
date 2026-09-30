-- =====================================================================
-- PYTHON MASTER 2026 - AI ASSISTANT (Module D1)
-- Script KHỞI TẠO DATABASE HOÀN CHỈNH (cài mới) - MySQL 8.0+
--
-- Chạy:  mysql -u root -p < sql/01_init_schema.sql
--   hoặc mở file trong MySQL Workbench / PyCharm Database tool -> Execute.
--
-- An toàn khi chạy lại nhiều lần (CREATE ... IF NOT EXISTS + seed có điều kiện).
-- Nếu đã có database phiên bản cũ (DB_PythonMaster.sql), dùng
-- sql/02_migration_v1_to_v2.sql thay vì file này.
--
-- Nhóm bảng:
--   1. Người dùng & kênh    : users, user_channel_identities, sites
--   2. Phiên & lịch sử chat : conversations, messages, VIEW chat_history
--   3. Knowledge Base (RAG) : faqs, knowledge_metadata, knowledge_chunks
--   4. Kiểm soát & handover : violation_logs, agent_notifications, support_tickets
-- =====================================================================

CREATE DATABASE IF NOT EXISTS gemini_chat_db
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE gemini_chat_db;

-- ---------------------------------------------------------------------
-- 1. USERS: thí sinh / tư vấn viên / quản trị.
--    PII (email, phone) chỉ được ghi khi người dùng đồng ý
--    (pii_consent_at != NULL) - theo mục 5 URD "Bảo mật & An toàn".
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    username        VARCHAR(50)  NOT NULL,
    display_name    VARCHAR(100) NULL,
    email           VARCHAR(255) NULL,
    phone           VARCHAR(20)  NULL,
    pii_consent_at  TIMESTAMP    NULL,
    role            ENUM('user', 'dev', 'staff', 'admin') NOT NULL DEFAULT 'user',
    is_active       TINYINT(1)   NOT NULL DEFAULT 1,
    last_seen_at    TIMESTAMP    NULL,
    created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_users_role (role),
    KEY idx_users_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 1b. USER_CHANNEL_IDENTITIES: ánh xạ ID người dùng bên ngoài
--     (Facebook PSID, Zalo user_id, visitor_id của Website Widget)
--     -> users.id nội bộ. Dùng ở Giai đoạn 4 (Webhook đa kênh).
--     1 user có thể có nhiều identity (cùng người, nhiều kênh).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_channel_identities (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    user_id           INT          NOT NULL,
    channel           ENUM('web', 'messenger', 'zalo') NOT NULL,
    -- Page ID (Messenger) / OA ID (Zalo) / site_key (Web). PSID của Facebook
    -- là duy nhất THEO TỪNG PAGE, nên phải nằm trong unique key.
    channel_account_id VARCHAR(128) NOT NULL DEFAULT '',
    external_user_id  VARCHAR(128) NOT NULL,
    created_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_channel_identity (channel, channel_account_id, external_user_id),
    KEY idx_identity_user (user_id),
    CONSTRAINT fk_identity_users FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sites (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    site_key        VARCHAR(100) NOT NULL UNIQUE,
    allowed_domain  VARCHAR(255) NOT NULL,
    is_active       TINYINT(1)   NOT NULL DEFAULT 1,
    created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 2. CONVERSATIONS = USER SESSION (phiên hội thoại).
--    status điều khiển luồng bot <-> tư vấn viên (D1-09 Handover).
--    history_summary: tóm tắt các lượt cũ để giảm token
--    (xem conversation_memory.py).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id                       INT AUTO_INCREMENT PRIMARY KEY,
    user_id                  INT          NOT NULL,
    site_id                  INT          NULL,
    channel                  ENUM('web', 'messenger', 'zalo') NOT NULL DEFAULT 'web',
    title                    VARCHAR(255) NOT NULL DEFAULT 'Đoạn chat mới',
    status                   ENUM('bot', 'waiting_agent', 'agent', 'closed')
                                          NOT NULL DEFAULT 'bot',
    fail_count               INT          NOT NULL DEFAULT 0,
    assigned_staff_id        INT          NULL,
    history_summary          TEXT         NULL,
    summary_covers_up_to_id  INT          NOT NULL DEFAULT 0,
    last_message_at          TIMESTAMP    NULL,
    closed_at                TIMESTAMP    NULL,
    created_at               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_conv_user_created (user_id, created_at),
    KEY idx_conv_status (status, created_at),
    CONSTRAINT fk_conversations_users FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_conversations_sites FOREIGN KEY (site_id)
        REFERENCES sites(id) ON DELETE SET NULL,
    CONSTRAINT fk_conversations_staff FOREIGN KEY (assigned_staff_id)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 2b. MESSAGES: bảng vật lý lưu LỊCH SỬ CHAT (giữ tên cũ để toàn bộ
--     code hiện có: routes/chat.py, routes/staff.py, conversation_memory.py
--     tiếp tục chạy mà không phải sửa).
--     Cột mới phục vụ kiểm soát hallucination (D1-10) & thống kê (D1-12):
--       answer_status       : answered | cannot_answer | out_of_scope | blocked | ...
--       retrieved_chunk_ids : JSON mảng id knowledge_chunks đã dùng làm ngữ cảnh
--       latency_ms          : thời gian sinh câu trả lời (NFR < 3 giây)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id                   INT AUTO_INCREMENT PRIMARY KEY,
    conversation_id      INT          NOT NULL,
    sender_type          ENUM('user', 'bot', 'model', 'staff', 'system') NOT NULL,
    content              TEXT         NOT NULL,
    image_url            VARCHAR(500) NULL,
    answer_status        VARCHAR(32)  NULL,
    retrieved_chunk_ids  JSON         NULL,
    latency_ms           INT          NULL,
    created_at           TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_messages_conv (conversation_id, id),
    KEY idx_messages_status (answer_status, created_at),
    CONSTRAINT fk_messages_conversations FOREIGN KEY (conversation_id)
        REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 2c. VIEW chat_history: góc nhìn "phẳng" theo user_id, dùng trực tiếp cho
--     endpoint GET /api/history/{user_id} (Giai đoạn 3) và báo cáo.
-- ---------------------------------------------------------------------
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
-- 3. FAQS: bộ FAQ chuẩn của Ban tổ chức (import từ danh_sach_faq.xlsx).
--    Được đồng bộ sang knowledge_chunks (source_type='faq') để truy vấn
--    vector cùng các tài liệu khác - xem knowledge/ingest_service.py.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS faqs (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    stt               INT          NULL,
    nhom_nghiep_vu    VARCHAR(100) NULL,
    intent            VARCHAR(150) NULL,
    tinh_huong        TEXT         NULL,
    cau_hoi_mau       TEXT         NULL,
    xu_ly_tinh_huong  TEXT         NULL,
    tra_loi_chuan     TEXT         NOT NULL,
    is_active         TINYINT(1)   NOT NULL DEFAULT 1,
    created_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                   ON UPDATE CURRENT_TIMESTAMP,
    FULLTEXT KEY ft_faq_search (cau_hoi_mau)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 3b. KNOWLEDGE_METADATA: metadata của mỗi tài liệu tri thức (PDF, TXT,
--     CSV, DOCX, MD, hoặc 1 "tài liệu ảo" FAQ).
--     content_hash (SHA-256 của file) UNIQUE -> chống nạp trùng.
--     status là vòng đời xử lý: pending -> processing -> ready | failed.
--     Chỉ tài liệu status='ready' AND is_active=1 mới được đưa vào RAG.
-- ---------------------------------------------------------------------
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

-- ---------------------------------------------------------------------
-- 3c. KNOWLEDGE_CHUNKS: đoạn văn bản đã cắt + vector embedding.
--     embedding lưu dạng BLOB float32 little-endian (768 chiều = 3072 byte),
--     đã chuẩn hoá L2 => cosine similarity = tích vô hướng.
--     Chạy được trên MySQL 8.0 phổ thông (không cần kiểu VECTOR của 9.x).
--     FULLTEXT ngram: đường dự phòng (keyword search) khi Embedding API
--     lỗi/hết quota; parser ngram xử lý tốt tiếng Việt có từ 2 ký tự
--     ("lệ", "thi") mà parser mặc định (min token 3) bỏ qua.
-- ---------------------------------------------------------------------
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

-- ---------------------------------------------------------------------
-- 4. KIỂM SOÁT NỘI DUNG (D1-07, D1-13) & HANDOVER (D1-09)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS violation_logs (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    conversation_id  INT          NOT NULL,
    user_id          INT          NULL,
    content          TEXT         NOT NULL,
    reason           VARCHAR(255) NULL,
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_violation_created (created_at),
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
    KEY idx_notif_unread (is_read, created_at),
    CONSTRAINT fk_agent_notifications_conversations FOREIGN KEY (conversation_id)
        REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 4b. SUPPORT_TICKETS: thí sinh để lại thông tin khi yêu cầu gặp tư vấn
--     viên NGOÀI GIỜ TRỰC (D1-09, TC-HANDOVER-08/09). Chỉ lưu tên/email/SĐT
--     khi thí sinh tích "đồng ý" trên form (URD mục 5).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS support_tickets (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    conversation_id  INT          NOT NULL,
    user_id          INT          NULL,
    full_name        VARCHAR(100) NOT NULL,
    email            VARCHAR(255) NULL,
    phone            VARCHAR(20)  NULL,
    content          TEXT         NOT NULL,
    status           ENUM('open', 'in_progress', 'resolved') NOT NULL DEFAULT 'open',
    handled_by       INT          NULL,
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_ticket_status (status, created_at),
    CONSTRAINT fk_ticket_conversations FOREIGN KEY (conversation_id)
        REFERENCES conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_ticket_users FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_ticket_staff FOREIGN KEY (handled_by)
        REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

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

-- ---------------------------------------------------------------------
-- 5. SEED DỮ LIỆU TỐI THIỂU (idempotent - chạy lại không nhân bản)
-- ---------------------------------------------------------------------
INSERT INTO sites (site_key, allowed_domain)
SELECT 'site_demo_001', '*'
WHERE NOT EXISTS (SELECT 1 FROM sites WHERE site_key = 'site_demo_001');

INSERT INTO users (username, display_name, role)
SELECT 'nhan_vien_demo', 'Tư vấn viên Demo', 'staff'
WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'nhan_vien_demo');
