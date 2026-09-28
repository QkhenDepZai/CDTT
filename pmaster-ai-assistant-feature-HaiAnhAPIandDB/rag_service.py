"""
RAG Service: câu hỏi -> truy vấn Knowledge Base -> ghép ngữ cảnh vào system
prompt -> gọi Gemini -> trả lời kèm nguồn trích dẫn.

    rag = get_rag_service()
    answer = rag.answer(question, history)            # text
    answer = rag.answer(question, history, image=...)  # text + ảnh (D1-02)

Tầng route (routes/chat.py) chỉ lo phiên chat, bảo mật, handover; toàn bộ
logic "trả lời dựa trên tri thức" nằm ở đây để các kênh khác (Messenger,
Zalo - Giai đoạn 4) gọi lại y hệt.

Chống hallucination (D1-10) theo 3 lớp:
1. Chỉ chunk có cosine >= RAG_MIN_SIMILARITY mới được làm ngữ cảnh.
2. Không tìm thấy gì -> vẫn gửi 1 khối RETRIEVED DATA ghi rõ "không có dữ
   liệu" để model BIẾT là phải dùng marker [KHONG_TIM_THAY] thay vì tự đoán.
3. System prompt cấm dùng kiến thức ngoài cho thông tin cuộc thi.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field

from config import (
    GEMINI_FALLBACK_MODEL_NAME,
    GEMINI_MODEL_NAME,
    RAG_FOLLOWUP_MAX_WORDS,
    RAG_MAX_CHARS_PER_FIELD,
    RAG_MAX_CONTEXT_CHARS,
    RAG_MIN_SIMILARITY,
    RAG_TOP_K,
)
from gemini_client import create_chat, send_message_with_image_retry, send_message_with_retry

logger = logging.getLogger("pmaster.rag")

# Lỗi phía Google (quá tải / hết quota) -> thử model dự phòng. Lỗi cấu hình
# (sai key, request sai) thì đổi model cũng không khỏi nên không chuyển.
FALLBACK_STATUSES = {"server_error", "rate_limited"}

NO_CONTEXT_NOTICE = (
    "(Không tìm thấy tài liệu nội bộ nào liên quan tới câu hỏi này. Nếu đây là câu hỏi "
    "về thông tin cuộc thi, KHÔNG được tự trả lời - hãy dùng marker không đủ thông tin.)"
)


@dataclass
class ImageInput:
    data: bytes
    mime_type: str


@dataclass
class RagSource:
    chunk_id: int | None
    document_id: int | None
    title: str
    citation: str
    score: float
    retrieval_method: str


@dataclass
class RagAnswer:
    reply: str
    status: str  # answered | cannot_answer | out_of_scope | rate_limited | server_error ...
    sources: list[RagSource] = field(default_factory=list)
    retrieval_ms: int = 0
    latency_ms: int = 0

    @property
    def chunk_ids(self) -> list[int]:
        """Mọi chunk đã đưa vào ngữ cảnh - lưu vào messages.retrieved_chunk_ids
        để truy vết khi AI trả lời sai (kể cả khi status != answered)."""
        return [s.chunk_id for s in self.sources if s.chunk_id is not None]

    def sources_payload(self) -> list[dict]:
        """Nguồn trích dẫn trả cho client: chỉ khi thực sự trả lời được, và
        không kèm nội dung chunk (không làm lộ Knowledge Base - D1-13)."""
        if self.status != "answered":
            return []
        return [
            {"title": s.title, "citation": s.citation, "score": s.score,
             "method": s.retrieval_method}
            for s in self.sources
        ]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["chunk_ids"] = self.chunk_ids
        return data


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


def _history_text(item) -> str:
    """Lấy text từ 1 phần tử history dạng {"role", "parts": [{"text"}]}."""
    parts = item.get("parts") or []
    return " ".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()


class RagService:
    def __init__(self, retriever=None, top_k: int = RAG_TOP_K,
                 min_score: float = RAG_MIN_SIMILARITY,
                 max_context_chars: int = RAG_MAX_CONTEXT_CHARS):
        self._retriever = retriever
        self.top_k = top_k
        self.min_score = min_score
        self.max_context_chars = max_context_chars

    @property
    def retriever(self):
        if self._retriever is None:
            from knowledge.retriever import get_retriever
            self._retriever = get_retriever()
        return self._retriever

    # ---- 1. Truy vấn -------------------------------------------------------
    @staticmethod
    def _build_retrieval_query(question: str, history: list[dict] | None) -> str:
        """Câu hỏi nối tiếp ngắn ("Còn Bảng B?", "Bảng A" - trả lời câu hỏi làm
        rõ D1-06) không tự đủ nghĩa để tìm tài liệu -> ghép câu hỏi gần nhất
        của người dùng vào truy vấn (chỉ để TÌM KIẾM, không đổi câu gửi Gemini)."""
        question = (question or "").strip()
        if not history or len(question.split()) >= RAG_FOLLOWUP_MAX_WORDS:
            return question
        for item in reversed(history):
            if item.get("role") == "user":
                previous = _history_text(item)
                if previous and not previous.startswith("[Tóm tắt"):
                    return f"{previous[:300]}\n{question}"
        return question

    def retrieve(self, query: str, cursor=None) -> list[tuple[RagSource, str]]:
        """Trả về list (RagSource, content). Vector KB là nguồn chính; nếu cả
        KB lỗi (DB/index) và có cursor -> dự phòng bằng FAQ keyword cũ."""
        try:
            chunks = self.retriever.search(query, top_k=self.top_k, min_score=self.min_score)
            return [
                (RagSource(c.chunk_id, c.document_id, c.title, c.citation(), c.score,
                           c.retrieval_method), c.content)
                for c in chunks
            ]
        except Exception as exc:  # noqa: BLE001 - không để lỗi KB làm sập luồng chat
            logger.error("[RAG] Knowledge Base lỗi, dùng FAQ keyword dự phòng: %s", exc)

        if cursor is None:
            return []
        try:
            from faq_matcher import find_faq_candidates
            rows = find_faq_candidates(cursor, query, limit=self.top_k)
        except Exception as exc:  # noqa: BLE001
            logger.error("[RAG] FAQ dự phòng cũng lỗi: %s", exc)
            return []

        results = []
        for row in rows:
            content = "\n".join(filter(None, [
                f"Chủ đề: {_truncate(row.get('intent'), RAG_MAX_CHARS_PER_FIELD)}",
                f"Câu hỏi mẫu: {_truncate(row.get('cau_hoi_mau'), RAG_MAX_CHARS_PER_FIELD)}",
                f"Trả lời chuẩn: {_truncate(row.get('tra_loi_chuan'), RAG_MAX_CHARS_PER_FIELD)}",
            ]))
            title = f"FAQ: {row.get('intent') or row.get('id')}"
            results.append((RagSource(None, None, title, title, 0.0, "faq_keyword"), content))
        return results

    # ---- 2. Ghép ngữ cảnh ----------------------------------------------------
    def build_context(self, retrieved: list[tuple[RagSource, str]]
                      ) -> tuple[str, list[RagSource]]:
        """Đánh số nguồn, cắt theo ngân sách ký tự. Trả về (context, nguồn đã dùng)."""
        if not retrieved:
            return NO_CONTEXT_NOTICE, []

        blocks, used, budget = [], [], self.max_context_chars
        for source, content in retrieved:
            header = f"[Nguồn {len(used) + 1}] {source.citation}"
            block = f"{header}\n{content.strip()}"
            if len(block) > budget:
                if used:
                    break
                # Luôn giữ ít nhất chunk tốt nhất (cắt bớt cho vừa ngân sách).
                block = _truncate(block, budget)
            blocks.append(block)
            used.append(source)
            budget -= len(block) + 2
        return "\n\n".join(blocks), used

    # ---- 3. Gọi Gemini -------------------------------------------------------
    @staticmethod
    def _generate(history, context, question, image, model=None, max_retries=None):
        chat = create_chat(history or [], retrieval_context=context, model=model)
        if image is not None:
            return send_message_with_image_retry(chat, question, image.data, image.mime_type,
                                                 max_retries=max_retries)
        return send_message_with_retry(chat, question, max_retries=max_retries)

    def answer(self, question: str, history: list[dict] | None = None,
               image: ImageInput | None = None, cursor=None,
               retrieval_query: str | None = None, intent_hint: str | None = None) -> RagAnswer:
        """retrieval_query / intent_hint: do disambiguation.resolve() cung cấp khi câu
        hỏi chứa cụm đa nghĩa ("điểm thi") -> tìm đúng tài liệu + nhắc Gemini đúng chủ đề."""
        started = time.perf_counter()
        question = (question or "").strip()

        retrieved = []
        if question:
            query = retrieval_query or self._build_retrieval_query(question, history)
            retrieved = self.retrieve(query, cursor=cursor)
        retrieval_ms = int((time.perf_counter() - started) * 1000)

        if question or not image:
            context, used_sources = self.build_context(retrieved)
            if intent_hint:
                context = f"[CHỦ ĐỀ ĐÃ XÁC ĐỊNH] {intent_hint}\n\n{context}"
        else:
            # Chỉ có ảnh, không có câu hỏi: không có gì để truy vấn KB;
            # dùng system prompt gốc (hỗ trợ đọc lỗi code trong ảnh - D1-02/05).
            context, used_sources = None, []

        reply, status = self._generate(history, context, question, image, model=None)
        if status in FALLBACK_STATUSES and GEMINI_FALLBACK_MODEL_NAME \
                and GEMINI_FALLBACK_MODEL_NAME != GEMINI_MODEL_NAME:
            logger.warning("[RAG] Model %s lỗi (%s) -> chuyển sang model dự phòng %s",
                           GEMINI_MODEL_NAME, status, GEMINI_FALLBACK_MODEL_NAME)
            reply, status = self._generate(history, context, question, image,
                                           model=GEMINI_FALLBACK_MODEL_NAME, max_retries=1)

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "[RAG] status=%s sources=%s top_score=%s retrieval_ms=%s latency_ms=%s",
            status, len(used_sources),
            used_sources[0].score if used_sources else None, retrieval_ms, latency_ms,
        )
        return RagAnswer(reply, status, used_sources, retrieval_ms, latency_ms)


_default_service: RagService | None = None
_default_lock = threading.Lock()


def get_rag_service() -> RagService:
    global _default_service
    if _default_service is None:
        with _default_lock:
            if _default_service is None:
                _default_service = RagService()
    return _default_service
