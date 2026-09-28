"""
Truy cập MySQL cho Knowledge Base: bảng knowledge_metadata & knowledge_chunks.

Theo đúng phong cách database.py hiện có (hàm nhận cursor/connection,
pymysql DictCursor, câu SQL tham số hoá %s -> chống SQL injection). Việc
commit/rollback do tầng service (ingest_service.py) quyết định để 1 lần
nạp tài liệu là 1 giao dịch trọn vẹn.
"""
from __future__ import annotations

import json
from typing import Iterable

import numpy as np

from knowledge.embedding_service import vector_to_blob
from knowledge.text_chunker import Chunk

DOCUMENT_COLUMNS = (
    "id, title, source_type, original_filename, storage_path, mime_type, "
    "file_size_bytes, content_hash, category, language, status, error_message, "
    "chunk_count, embedding_model, embedding_dim, is_active, uploaded_by, "
    "processed_at, created_at, updated_at"
)

MAX_ERROR_MESSAGE_LEN = 1000


# ---- knowledge_metadata -----------------------------------------------------
def find_document_by_hash(cursor, content_hash: str):
    # minutes_since_update tính ngay trong MySQL (cùng múi giờ với updated_at),
    # dùng để phát hiện bản ghi kẹt ở 'processing'.
    cursor.execute(
        f"SELECT {DOCUMENT_COLUMNS}, TIMESTAMPDIFF(MINUTE, updated_at, NOW()) "
        "AS minutes_since_update FROM knowledge_metadata WHERE content_hash = %s",
        (content_hash,),
    )
    return cursor.fetchone()


def get_document(cursor, document_id: int):
    cursor.execute(
        f"SELECT {DOCUMENT_COLUMNS} FROM knowledge_metadata WHERE id = %s",
        (document_id,),
    )
    return cursor.fetchone()


def get_faq_document(cursor):
    """Tài liệu ảo đại diện cho bảng faqs (chỉ có đúng 1 bản ghi)."""
    cursor.execute(
        f"SELECT {DOCUMENT_COLUMNS} FROM knowledge_metadata "
        "WHERE source_type = 'faq' ORDER BY id LIMIT 1"
    )
    return cursor.fetchone()


def list_documents(cursor, limit: int = 50, offset: int = 0, include_inactive: bool = True):
    where = "" if include_inactive else "WHERE is_active = 1"
    cursor.execute(
        f"SELECT {DOCUMENT_COLUMNS} FROM knowledge_metadata {where} "
        "ORDER BY id DESC LIMIT %s OFFSET %s",
        (limit, offset),
    )
    return cursor.fetchall()


def create_document(
    cursor,
    *,
    title: str,
    source_type: str,
    content_hash: str,
    original_filename: str | None = None,
    storage_path: str | None = None,
    mime_type: str | None = None,
    file_size_bytes: int | None = None,
    category: str | None = None,
    language: str = "vi",
    uploaded_by: int | None = None,
) -> int:
    cursor.execute(
        "INSERT INTO knowledge_metadata (title, source_type, original_filename, "
        "storage_path, mime_type, file_size_bytes, content_hash, category, language, "
        "status, uploaded_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'processing', %s)",
        (title[:255], source_type, original_filename, storage_path, mime_type,
         file_size_bytes, content_hash, category, language, uploaded_by),
    )
    return cursor.lastrowid


def mark_processing(cursor, document_id: int, content_hash: str | None = None):
    if content_hash:
        cursor.execute(
            "UPDATE knowledge_metadata SET status = 'processing', error_message = NULL, "
            "content_hash = %s WHERE id = %s",
            (content_hash, document_id),
        )
    else:
        cursor.execute(
            "UPDATE knowledge_metadata SET status = 'processing', error_message = NULL "
            "WHERE id = %s",
            (document_id,),
        )


def mark_ready(cursor, document_id: int, chunk_count: int, embedding_model: str,
               embedding_dim: int):
    cursor.execute(
        "UPDATE knowledge_metadata SET status = 'ready', error_message = NULL, "
        "chunk_count = %s, embedding_model = %s, embedding_dim = %s, "
        "processed_at = NOW() WHERE id = %s",
        (chunk_count, embedding_model, embedding_dim, document_id),
    )


def mark_failed(cursor, document_id: int, error_message: str):
    cursor.execute(
        "UPDATE knowledge_metadata SET status = 'failed', error_message = %s "
        "WHERE id = %s",
        ((error_message or "")[:MAX_ERROR_MESSAGE_LEN], document_id),
    )


def set_document_active(cursor, document_id: int, is_active: bool) -> int:
    cursor.execute(
        "UPDATE knowledge_metadata SET is_active = %s WHERE id = %s",
        (1 if is_active else 0, document_id),
    )
    return cursor.rowcount


def delete_document(cursor, document_id: int) -> int:
    """Xoá hẳn tài liệu; knowledge_chunks bị xoá theo nhờ ON DELETE CASCADE."""
    cursor.execute("DELETE FROM knowledge_metadata WHERE id = %s", (document_id,))
    return cursor.rowcount


# ---- knowledge_chunks -------------------------------------------------------
def delete_chunks(cursor, document_id: int) -> int:
    cursor.execute("DELETE FROM knowledge_chunks WHERE document_id = %s", (document_id,))
    return cursor.rowcount


def insert_chunks(cursor, document_id: int, chunks: list[Chunk], vectors: np.ndarray,
                  embedding_model: str) -> int:
    if len(chunks) != len(vectors):
        raise ValueError("Số chunk và số vector không khớp")

    rows = [
        (
            document_id,
            chunk.index,
            chunk.content,
            chunk.content_hash,
            chunk.char_count,
            chunk.token_estimate,
            chunk.page_number,
            json.dumps(chunk.metadata, ensure_ascii=False) if chunk.metadata else None,
            vector_to_blob(vector),
            embedding_model,
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    cursor.executemany(
        "INSERT INTO knowledge_chunks (document_id, chunk_index, content, content_hash, "
        "char_count, token_estimate, page_number, metadata, embedding, embedding_model) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        rows,
    )
    return len(rows)


def fetch_active_embeddings(cursor, embedding_model: str, batch_size: int = 5000
                            ) -> Iterable[list[dict]]:
    """Duyệt theo lô (keyset pagination) các vector của tài liệu đang dùng.

    Chỉ lấy chunk sinh bởi đúng embedding_model hiện tại: vector của model
    khác nằm ở không gian khác, trộn vào sẽ cho điểm cosine vô nghĩa.
    """
    last_id = 0
    while True:
        cursor.execute(
            "SELECT kc.id, kc.embedding FROM knowledge_chunks kc "
            "JOIN knowledge_metadata km ON km.id = kc.document_id "
            "WHERE km.status = 'ready' AND km.is_active = 1 "
            "AND kc.embedding IS NOT NULL AND kc.embedding_model = %s AND kc.id > %s "
            "ORDER BY kc.id LIMIT %s",
            (embedding_model, last_id, batch_size),
        )
        rows = cursor.fetchall()
        if not rows:
            return
        yield rows
        last_id = rows[-1]["id"]


def fetch_chunks_by_ids(cursor, chunk_ids: list[int]) -> dict[int, dict]:
    if not chunk_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(chunk_ids))
    cursor.execute(
        "SELECT kc.id, kc.document_id, kc.chunk_index, kc.content, kc.page_number, "
        "kc.metadata, km.title, km.source_type, km.category "
        "FROM knowledge_chunks kc "
        "JOIN knowledge_metadata km ON km.id = kc.document_id "
        f"WHERE kc.id IN ({placeholders}) "
        "AND km.status = 'ready' AND km.is_active = 1",
        tuple(chunk_ids),
    )
    return {row["id"]: row for row in cursor.fetchall()}


def fulltext_search_chunks(cursor, query: str, limit: int) -> list[dict]:
    """Tìm kiếm từ khoá (FULLTEXT ngram) - đường dự phòng khi không embed được
    câu hỏi (Embedding API lỗi/hết quota), để chatbot vẫn trả lời theo KB."""
    cursor.execute(
        "SELECT kc.id, kc.document_id, kc.chunk_index, kc.content, kc.page_number, "
        "kc.metadata, km.title, km.source_type, km.category, "
        "MATCH(kc.content) AGAINST (%s IN NATURAL LANGUAGE MODE) AS score "
        "FROM knowledge_chunks kc "
        "JOIN knowledge_metadata km ON km.id = kc.document_id "
        "WHERE km.status = 'ready' AND km.is_active = 1 "
        "AND MATCH(kc.content) AGAINST (%s IN NATURAL LANGUAGE MODE) "
        "ORDER BY score DESC LIMIT %s",
        (query, query, limit),
    )
    return cursor.fetchall()


def get_index_signature(cursor, embedding_model: str) -> tuple:
    """Dấu vân tay trạng thái KB. Đổi => chỉ mục vector trong RAM đã cũ."""
    cursor.execute(
        "SELECT COUNT(*) AS docs, COALESCE(SUM(chunk_count), 0) AS chunks, "
        "COALESCE(MAX(id), 0) AS max_id, MAX(updated_at) AS last_update "
        "FROM knowledge_metadata "
        "WHERE status = 'ready' AND is_active = 1 AND embedding_model = %s",
        (embedding_model,),
    )
    row = cursor.fetchone() or {}
    return (
        int(row.get("docs") or 0),
        int(row.get("chunks") or 0),
        int(row.get("max_id") or 0),
        str(row.get("last_update")),
    )


# ---- faqs (nguồn cho tài liệu ảo source_type='faq') --------------------------
def fetch_active_faqs(cursor) -> list[dict]:
    cursor.execute(
        "SELECT id, nhom_nghiep_vu, intent, tinh_huong, cau_hoi_mau, xu_ly_tinh_huong, "
        "tra_loi_chuan "
        "FROM faqs WHERE is_active = 1 ORDER BY id"
    )
    return cursor.fetchall()
