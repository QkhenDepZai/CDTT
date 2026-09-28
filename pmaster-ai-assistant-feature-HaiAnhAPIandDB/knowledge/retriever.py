"""
Truy vấn Knowledge Base: câu hỏi -> top-K chunk liên quan kèm nguồn.

Đây là điểm vào duy nhất mà tầng RAG (Giai đoạn 2 - rag_service.py) cần gọi.

Chiến lược:
1. Vector search (Gemini Embedding + cosine) - hiểu ngữ nghĩa, bắt được câu
   hỏi diễn đạt khác với tài liệu.
2. Nếu KHÔNG embed được câu hỏi (API lỗi/hết quota) -> tự rơi về FULLTEXT
   ngram của MySQL, để chatbot vẫn trả lời theo KB thay vì báo lỗi.
3. Chunk có điểm < RAG_MIN_SIMILARITY bị loại: thà trả về rỗng (để AI nói
   "không rõ, liên hệ tư vấn viên") còn hơn đưa ngữ cảnh sai (D1-10).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass

from config import RAG_MIN_SIMILARITY, RAG_TOP_K, RAG_INDEX_REFRESH_SECONDS
from database import get_db_connection
from knowledge import repository
from knowledge.embedding_service import EmbeddingError, EmbeddingService, get_embedding_service
from knowledge.vector_index import VectorIndex

logger = logging.getLogger("pmaster.knowledge.retriever")


@dataclass
class RetrievedChunk:
    chunk_id: int
    document_id: int
    title: str
    source_type: str
    content: str
    score: float
    page_number: int | None = None
    category: str | None = None
    metadata: dict | None = None
    retrieval_method: str = "vector"

    def to_dict(self) -> dict:
        return asdict(self)

    def citation(self) -> str:
        """Nhãn nguồn ngắn gọn để hiển thị/ghi log, ví dụ 'Thể lệ 2026 (tr.3)'."""
        if self.page_number:
            return f"{self.title} (tr.{self.page_number})"
        row = (self.metadata or {}).get("row_number")
        if row:
            return f"{self.title} (dòng {row})"
        return self.title


def _parse_metadata(raw) -> dict | None:
    if raw is None or isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


class KnowledgeRetriever:
    def __init__(self, embedding_service: EmbeddingService | None = None,
                 refresh_seconds: int = RAG_INDEX_REFRESH_SECONDS):
        self.embeddings = embedding_service or get_embedding_service()
        self.index = VectorIndex(self.embeddings.dimension)
        self.refresh_seconds = refresh_seconds
        self._last_check = 0.0
        self._reload_lock = threading.Lock()
        self._loaded = False

    # ---- đồng bộ chỉ mục RAM với MySQL ----------------------------------------
    def reload(self, cursor=None) -> int:
        with self._reload_lock:
            if cursor is not None:
                count = self.index.load_from_db(cursor, self.embeddings.model)
            else:
                connection = get_db_connection()
                try:
                    with connection.cursor() as own_cursor:
                        count = self.index.load_from_db(own_cursor, self.embeddings.model)
                finally:
                    connection.close()
            self._loaded = True
            self._last_check = time.monotonic()
            return count

    def invalidate(self):
        """Gọi ngay sau khi nạp/xoá tài liệu trong CÙNG tiến trình."""
        self._loaded = False

    def _ensure_fresh(self, cursor):
        if not self._loaded:
            self.reload(cursor)
            return
        if time.monotonic() - self._last_check < self.refresh_seconds:
            return
        # Tiến trình/worker khác có thể vừa upload tài liệu -> so dấu vân tay.
        self._last_check = time.monotonic()
        signature = repository.get_index_signature(cursor, self.embeddings.model)
        if signature != self.index.signature:
            logger.info("[Retriever] KB đã thay đổi, nạp lại chỉ mục vector.")
            self.reload(cursor)

    # ---- truy vấn -------------------------------------------------------------
    def search(self, query: str, top_k: int = RAG_TOP_K,
               min_score: float = RAG_MIN_SIMILARITY) -> list[RetrievedChunk]:
        query = (query or "").strip()
        if not query:
            return []

        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_fresh(cursor)
                try:
                    query_vector = self.embeddings.embed_query(query)
                except EmbeddingError as exc:
                    logger.warning(
                        "[Retriever] Không embed được câu hỏi (%s) -> dùng FULLTEXT.", exc,
                    )
                    return self._fulltext_fallback(cursor, query, top_k)

                hits = self.index.search(query_vector, top_k=top_k, min_score=min_score)
                if not hits:
                    return []
                rows = repository.fetch_chunks_by_ids(cursor, [chunk_id for chunk_id, _ in hits])
        finally:
            connection.close()

        results = []
        for chunk_id, score in hits:
            row = rows.get(chunk_id)
            if row is None:
                # Tài liệu vừa bị tắt/xoá sau lần nạp chỉ mục gần nhất.
                self.invalidate()
                continue
            results.append(self._to_result(row, score, "vector"))
        return results

    def _fulltext_fallback(self, cursor, query: str, top_k: int) -> list[RetrievedChunk]:
        try:
            rows = repository.fulltext_search_chunks(cursor, query, top_k)
        except Exception as exc:  # noqa: BLE001 - fallback không được làm sập luồng chat
            logger.error("[Retriever] FULLTEXT fallback lỗi: %s", exc)
            return []
        return [self._to_result(row, float(row.get("score") or 0.0), "fulltext") for row in rows]

    @staticmethod
    def _to_result(row: dict, score: float, method: str) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=row["id"],
            document_id=row["document_id"],
            title=row["title"],
            source_type=row["source_type"],
            content=row["content"],
            score=round(score, 4),
            page_number=row.get("page_number"),
            category=row.get("category"),
            metadata=_parse_metadata(row.get("metadata")),
            retrieval_method=method,
        )


_default_retriever: KnowledgeRetriever | None = None
_default_lock = threading.Lock()


def get_retriever() -> KnowledgeRetriever:
    global _default_retriever
    if _default_retriever is None:
        with _default_lock:
            if _default_retriever is None:
                _default_retriever = KnowledgeRetriever()
    return _default_retriever
