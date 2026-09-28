"""
GET /api/health - kiểm tra nhanh tình trạng hệ thống (dùng cho Postman,
giám sát uptime NFR 99.9%, load balancer). Không trả thông tin nhạy cảm
(không có API key, mật khẩu, host DB).
"""
import logging

from flask import Blueprint, jsonify

from config import EMBEDDING_MODEL_NAME, GEMINI_MODEL_NAME
from database import ping_database

logger = logging.getLogger("pmaster.health")

health_bp = Blueprint('health', __name__)


@health_bp.route('/api/health', methods=['GET'])
def health():
    db_ok, db_error = ping_database()
    if not db_ok:
        logger.error("[Health] MySQL lỗi: %s", db_error)

    kb_chunks = None
    if db_ok:
        try:
            from knowledge.retriever import get_retriever
            kb_chunks = get_retriever().index.size
        except Exception as exc:  # noqa: BLE001 - health check không được tự sập
            logger.error("[Health] Không đọc được chỉ mục KB: %s", exc)

    body = {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "error",
        "knowledge_base_chunks_loaded": kb_chunks,
        "llm_model": GEMINI_MODEL_NAME,
        "embedding_model": EMBEDDING_MODEL_NAME,
    }
    return jsonify(body), 200 if db_ok else 503
