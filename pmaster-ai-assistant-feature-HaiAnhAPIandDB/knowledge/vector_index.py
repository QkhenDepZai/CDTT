"""
Chỉ mục vector trong RAM (numpy) cho truy vấn cosine similarity.

Vì sao không truy vấn vector thẳng trong MySQL?
- MySQL 8.0 (bản phổ biến trên máy dev/XAMPP) không có kiểu VECTOR hay hàm
  khoảng cách; tính cosine bằng SQL trên BLOB sẽ quét toàn bảng rất chậm.
- KB của cuộc thi (thể lệ, quy chế, FAQ, đề mẫu) cỡ vài nghìn chunk:
  10.000 chunk x 768 chiều x 4 byte ~ 30 MB RAM, 1 phép nhân ma trận mất
  < 5 ms -> đủ nhanh cho NFR < 3 giây mà không cần thêm hạ tầng.
- MySQL vẫn là nguồn dữ liệu gốc; chỉ mục này chỉ là bản sao đọc nhanh,
  nạp lại được bất cứ lúc nào (load_from_db).

Khi KB vượt ~200.000 chunk: thay lớp này bằng Qdrant/pgvector/MySQL HeatWave
nhưng giữ nguyên giao diện search() để các tầng trên không phải sửa.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from knowledge import repository
from knowledge.embedding_service import blob_to_vector

logger = logging.getLogger("pmaster.knowledge.index")


class VectorIndex:
    def __init__(self, dimension: int):
        self.dimension = dimension
        self._lock = threading.RLock()
        self._matrix = np.empty((0, dimension), dtype=np.float32)
        self._chunk_ids = np.empty((0,), dtype=np.int64)
        self.signature: tuple | None = None

    @property
    def size(self) -> int:
        return int(self._chunk_ids.shape[0])

    def load_from_db(self, cursor, embedding_model: str) -> int:
        """Nạp lại toàn bộ vector đang hiệu lực. Dựng ma trận mới xong mới
        hoán đổi (swap) nên các truy vấn đang chạy không bị ảnh hưởng."""
        signature = repository.get_index_signature(cursor, embedding_model)
        ids: list[int] = []
        vectors: list[np.ndarray] = []
        skipped = 0

        for rows in repository.fetch_active_embeddings(cursor, embedding_model):
            for row in rows:
                vector = blob_to_vector(row["embedding"])
                if vector.shape[0] != self.dimension:
                    skipped += 1
                    continue
                ids.append(row["id"])
                vectors.append(vector)

        matrix = (
            np.vstack(vectors).astype(np.float32, copy=False)
            if vectors else np.empty((0, self.dimension), dtype=np.float32)
        )
        with self._lock:
            self._matrix = matrix
            self._chunk_ids = np.asarray(ids, dtype=np.int64)
            self.signature = signature

        if skipped:
            logger.warning(
                "[VectorIndex] Bỏ qua %s chunk sai số chiều (cần reindex).", skipped,
            )
        logger.info("[VectorIndex] Đã nạp %s vector (model=%s).", len(ids), embedding_model)
        return len(ids)

    def search(self, query_vector: np.ndarray, top_k: int,
               min_score: float = -1.0) -> list[tuple[int, float]]:
        """Trả về [(chunk_id, cosine_score)] giảm dần, đã lọc theo min_score."""
        with self._lock:
            matrix = self._matrix
            chunk_ids = self._chunk_ids

        if matrix.shape[0] == 0 or top_k <= 0:
            return []

        query = np.asarray(query_vector, dtype=np.float32)
        norm = np.linalg.norm(query)
        if norm == 0:
            return []
        scores = matrix @ (query / norm)

        k = min(top_k, scores.shape[0])
        # argpartition O(n) rồi mới sort k phần tử -> nhanh hơn sort toàn bộ.
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [
            (int(chunk_ids[i]), float(scores[i]))
            for i in top
            if scores[i] >= min_score
        ]
