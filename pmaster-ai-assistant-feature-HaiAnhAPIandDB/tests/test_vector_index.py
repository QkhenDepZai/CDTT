import numpy as np

from knowledge.embedding_service import blob_to_vector, l2_normalize, vector_to_blob
from knowledge.vector_index import VectorIndex


def _index_with(vectors, ids):
    index = VectorIndex(dimension=len(vectors[0]))
    index._matrix = l2_normalize(np.asarray(vectors, dtype=np.float32))
    index._chunk_ids = np.asarray(ids, dtype=np.int64)
    return index


def test_blob_roundtrip_is_lossless():
    vector = l2_normalize(np.random.default_rng(1).normal(size=768))
    blob = vector_to_blob(vector)
    assert len(blob) == 768 * 4
    np.testing.assert_array_equal(blob_to_vector(blob), vector)


def test_search_orders_by_cosine_and_applies_threshold():
    index = _index_with([[1, 0, 0], [0.9, 0.1, 0], [0, 1, 0]], [10, 20, 30])
    hits = index.search(np.array([1, 0, 0]), top_k=3, min_score=0.5)
    assert [chunk_id for chunk_id, _ in hits] == [10, 20]
    assert hits[0][1] > hits[1][1]


def test_search_top_k_larger_than_index():
    index = _index_with([[1, 0], [0, 1]], [1, 2])
    assert len(index.search(np.array([1, 1]), top_k=10)) == 2


def test_empty_index_and_zero_query():
    assert VectorIndex(3).search(np.array([1, 0, 0]), top_k=3) == []
    index = _index_with([[1, 0, 0]], [1])
    assert index.search(np.zeros(3), top_k=1) == []
