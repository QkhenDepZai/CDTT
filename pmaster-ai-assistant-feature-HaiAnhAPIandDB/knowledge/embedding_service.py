"""
Sinh vector embedding bằng Google Gemini Embedding (thư viện google-genai).

- Batch nhiều đoạn trong 1 request (EMBEDDING_BATCH_SIZE) để giảm số lần
  gọi API khi nạp tài liệu lớn.
- Retry có backoff CHỈ với lỗi tạm thời (429 / 5xx / timeout); lỗi cấu hình
  (400/401/403/404) raise ngay vì retry cũng không tự khỏi.
- Chuẩn hoá L2 mọi vector: gemini-embedding-001 chỉ tự chuẩn hoá ở 3072
  chiều; với 768/1536 phải tự chuẩn hoá thì cosine = tích vô hướng mới đúng.
- Cache embedding của câu hỏi (LRU) -> câu hỏi lặp lại (rất phổ biến với
  FAQ cuộc thi) không tốn thêm 1 lần gọi API, giúp đạt NFR < 3 giây.
- Không log API key hay nội dung văn bản người dùng.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

import numpy as np
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import (
    GEMINI_API_KEY,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_DIMENSION,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_TIMEOUT_MS,
    EMBEDDING_MAX_RETRIES,
)

logger = logging.getLogger("pmaster.knowledge.embedding")

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
QUERY_CACHE_SIZE = 1024
# Giới hạn an toàn độ dài 1 đoạn gửi đi (model giới hạn 2048 token đầu vào).
MAX_INPUT_CHARS = 6000


class EmbeddingError(Exception):
    """Không sinh được embedding (đã hết số lần thử hoặc lỗi không thể retry)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class EmbeddingService:
    def __init__(
        self,
        model: str = EMBEDDING_MODEL_NAME,
        dimension: int = EMBEDDING_DIMENSION,
        batch_size: int = EMBEDDING_BATCH_SIZE,
        max_retries: int = EMBEDDING_MAX_RETRIES,
        client: genai.Client | None = None,
    ):
        self.model = model
        self.dimension = dimension
        self.batch_size = max(1, min(batch_size, 100))
        self.max_retries = max(0, max_retries)
        self._client = client
        self._client_lock = threading.Lock()
        self._query_cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_lock = threading.Lock()
        # gemini-embedding-2 không nhận task_type -> ghi task bằng tiền tố.
        self._uses_task_prefix = model.startswith("gemini-embedding-2")

    # ---- client khởi tạo lười: import module không cần API key (unit test) --
    @property
    def client(self) -> genai.Client:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    if not GEMINI_API_KEY:
                        raise EmbeddingError("Chưa cấu hình GEMINI_API_KEY trong file .env")
                    self._client = genai.Client(
                        api_key=GEMINI_API_KEY,
                        http_options=types.HttpOptions(
                            timeout=EMBEDDING_TIMEOUT_MS,
                            # Tắt retry ngầm của SDK, retry do _embed_batch quản lý
                            # (cùng lý do như gemini_client.py: tránh double-retry).
                            retry_options=types.HttpRetryOptions(attempts=1),
                        ),
                    )
        return self._client

    # ---- định dạng đầu vào theo task --------------------------------------
    def _format_document(self, text: str, title: str | None) -> str:
        text = text[:MAX_INPUT_CHARS]
        if self._uses_task_prefix:
            return f"title: {title or 'none'} | text: {text}"
        # Với embedding-001: thêm tiêu đề tài liệu vào đầu chunk để vector
        # mang ngữ cảnh "đoạn này thuộc tài liệu nào" (contextual chunk).
        return f"{title}\n{text}" if title else text

    def _format_query(self, text: str) -> str:
        text = text[:MAX_INPUT_CHARS]
        if self._uses_task_prefix:
            return f"task: search result | query: {text}"
        return text

    def _build_config(self, task_type: str) -> types.EmbedContentConfig:
        if self._uses_task_prefix:
            return types.EmbedContentConfig(output_dimensionality=self.dimension)
        return types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=self.dimension,
        )

    # ---- gọi API có retry -------------------------------------------------
    def _embed_batch(self, texts: list[str], task_type: str) -> np.ndarray:
        config = self._build_config(task_type)
        max_attempts = self.max_retries + 1

        for attempt in range(1, max_attempts + 1):
            try:
                response = self.client.models.embed_content(
                    model=self.model, contents=texts, config=config,
                )
                vectors = [embedding.values for embedding in (response.embeddings or [])]
                if len(vectors) != len(texts):
                    raise EmbeddingError(
                        f"API trả về {len(vectors)} vector cho {len(texts)} đoạn văn"
                    )
                matrix = np.asarray(vectors, dtype=np.float32)
                if matrix.shape[1] != self.dimension:
                    raise EmbeddingError(
                        f"Số chiều vector {matrix.shape[1]} khác cấu hình {self.dimension}"
                    )
                return l2_normalize(matrix)
            except EmbeddingError:
                raise
            except genai_errors.APIError as exc:
                code = getattr(exc, "code", None)
                logger.warning(
                    "[Embedding] model=%s attempt=%s/%s status_code=%s error=%s",
                    self.model, attempt, max_attempts, code, getattr(exc, "message", exc),
                )
                if code not in RETRYABLE_STATUS_CODES or attempt == max_attempts:
                    raise EmbeddingError(f"Gemini Embedding API lỗi {code}", code) from exc
                # 429 = hết quota theo phút -> chờ lâu hơn lỗi 5xx thoáng qua.
                base = 5 if code == 429 else 1
                time.sleep(min(base * 2 ** (attempt - 1), 30))
            except Exception as exc:  # noqa: BLE001 - lỗi mạng/timeout của httpx
                logger.warning(
                    "[Embedding] model=%s attempt=%s/%s error_type=%s error=%s",
                    self.model, attempt, max_attempts, type(exc).__name__, exc,
                )
                if attempt == max_attempts:
                    raise EmbeddingError(f"Không kết nối được Gemini Embedding API: {exc}") from exc
                time.sleep(min(2 ** (attempt - 1), 30))

        raise EmbeddingError("Hết số lần thử gọi Embedding API")

    # ---- API công khai -----------------------------------------------------
    def embed_documents(self, texts: list[str], title: str | None = None) -> np.ndarray:
        """Embed danh sách chunk để LƯU vào KB. Trả về ma trận (n, dimension)."""
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)

        formatted = [self._format_document(text, title) for text in texts]
        batches = []
        for start in range(0, len(formatted), self.batch_size):
            batch = formatted[start:start + self.batch_size]
            batches.append(self._embed_batch(batch, "RETRIEVAL_DOCUMENT"))
            logger.info(
                "[Embedding] đã embed %s/%s chunk", min(start + len(batch), len(formatted)),
                len(formatted),
            )
        return np.vstack(batches)

    def embed_query(self, text: str) -> np.ndarray:
        """Embed câu hỏi người dùng để TRUY VẤN. Trả về vector (dimension,)."""
        key = " ".join((text or "").lower().split())
        if not key:
            raise EmbeddingError("Câu hỏi rỗng")

        with self._cache_lock:
            cached = self._query_cache.get(key)
            if cached is not None:
                self._query_cache.move_to_end(key)
                return cached

        vector = self._embed_batch([self._format_query(text)], "RETRIEVAL_QUERY")[0]
        with self._cache_lock:
            self._query_cache[key] = vector
            if len(self._query_cache) > QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)
        return vector


def vector_to_blob(vector: np.ndarray) -> bytes:
    """float32 little-endian -> BLOB MySQL (768 chiều = 3072 byte)."""
    return np.asarray(vector, dtype="<f4").tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


_default_service: EmbeddingService | None = None
_default_lock = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    """Singleton dùng chung trong 1 tiến trình (giữ cache câu hỏi)."""
    global _default_service
    if _default_service is None:
        with _default_lock:
            if _default_service is None:
                _default_service = EmbeddingService()
    return _default_service
