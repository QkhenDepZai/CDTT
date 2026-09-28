"""Bản giả lập dùng chung cho test (không gọi Gemini thật)."""
import hashlib
import re

import numpy as np


class FakeEmbeddingService:
    """Embedding giả lập tất định: bag-of-words băm vào 768 chiều.
    Câu hỏi có nhiều từ chung với chunk -> cosine cao, đủ để kiểm chứng luồng
    truy vấn mà không tốn quota Gemini."""

    model = "fake-embedding"
    dimension = 768

    def __init__(self, fail_queries=False):
        self.fail_queries = fail_queries
        self.document_calls = 0

    def _vector(self, text):
        vector = np.zeros(self.dimension, dtype=np.float32)
        for word in re.findall(r"\w+", text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimension] += 1.0
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector

    def embed_documents(self, texts, title=None):
        self.document_calls += 1
        return np.vstack([self._vector(text) for text in texts])

    def embed_query(self, text):
        from knowledge.embedding_service import EmbeddingError
        if self.fail_queries:
            raise EmbeddingError("quota exceeded", 429)
        return self._vector(text)
