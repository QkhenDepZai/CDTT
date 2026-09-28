"""
API quản trị Knowledge Base (D1-11) - yêu cầu header X-Admin-Key.

    POST   /api/knowledge/upload         multipart: file, title?, category?, background?
    POST   /api/knowledge/text           JSON: title, content, category? (nhập trực tiếp)
    POST   /api/knowledge/sync-faq       ?force=true  đồng bộ bảng faqs -> KB
    GET    /api/knowledge                ?limit=&offset=  danh sách tài liệu
    GET    /api/knowledge/<id>           chi tiết / theo dõi trạng thái xử lý
    PATCH  /api/knowledge/<id>           JSON: {"is_active": false}
    POST   /api/knowledge/<id>/reindex   embed lại
    DELETE /api/knowledge/<id>
    POST   /api/knowledge/search         JSON: query, top_k?, min_score? (hiệu chỉnh RAG)

Dữ liệu mới có hiệu lực NGAY sau khi xử lý xong (chỉ mục vector được làm
mới trong tiến trình hiện tại, và các worker khác sau RAG_INDEX_REFRESH_SECONDS).
"""
import logging

from flask import Blueprint, jsonify, request

from config import KNOWLEDGE_MAX_FILE_MB
from database import get_db_connection
from knowledge import ingest_service, repository
from security import require_admin_key

logger = logging.getLogger("pmaster.knowledge_api")

knowledge_bp = Blueprint('knowledge', __name__)

STATUS_HTTP_CODES = {
    "created": 201,
    "updated": 200,
    "duplicate": 200,
    "unchanged": 200,
    "processing": 202,
}
FAILURE_HTTP_CODES = {
    "validation": 422,
    "not_found": 404,
    "embedding": 503,
    "system": 500,
}
MAX_TEXT_CHARS = 200_000
HIDDEN_DOCUMENT_FIELDS = {"storage_path", "minutes_since_update"}


@knowledge_bp.before_request
def _check_admin():
    if request.method == "OPTIONS":
        return None
    return require_admin_key()


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _result_response(result):
    # "status" = thành công/lỗi của request; "result" = kết quả xử lý tài liệu
    # (created | updated | duplicate | unchanged | processing | failed).
    body = result.to_dict()
    body["result"] = body.pop("status")
    body = {"status": "success" if result.ok else "error", **body}
    if result.ok:
        return jsonify(body), STATUS_HTTP_CODES.get(result.status, 200)
    return jsonify(body), FAILURE_HTTP_CODES.get(result.reason, 422)


def _serialize_document(row):
    document = {k: v for k, v in dict(row).items() if k not in HIDDEN_DOCUMENT_FIELDS}
    for key in ("processed_at", "created_at", "updated_at"):
        if document.get(key) is not None:
            document[key] = document[key].isoformat(sep=" ")
    document["is_active"] = bool(document.get("is_active"))
    return document


def _parse_id_json():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Body phải là JSON object"}), 400)
    return data, None


@knowledge_bp.route('/api/knowledge/upload', methods=['POST'])
def upload_document():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Thiếu file (form-data, key 'file')."}), 400

    raw = uploaded.read(KNOWLEDGE_MAX_FILE_MB * 1024 * 1024 + 1)
    result = ingest_service.ingest_bytes(
        raw,
        uploaded.filename,
        title=(request.form.get("title") or "").strip() or None,
        category=(request.form.get("category") or "").strip() or None,
        background=_truthy(request.form.get("background")),
    )
    logger.info("[KnowledgeAPI] upload '%s' -> %s #%s", uploaded.filename, result.status,
                result.document_id)
    return _result_response(result)


@knowledge_bp.route('/api/knowledge/text', methods=['POST'])
def create_text_document():
    """Nhập tri thức trực tiếp không cần file (ví dụ thông báo mới của BTC)."""
    data, error = _parse_id_json()
    if error:
        return error
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "").strip()
    if not title or not content:
        return jsonify({"error": "Cần 'title' và 'content'."}), 400
    if len(content) > MAX_TEXT_CHARS:
        return jsonify({"error": f"'content' tối đa {MAX_TEXT_CHARS} ký tự."}), 400

    result = ingest_service.ingest_bytes(
        content.encode("utf-8"),
        f"{ingest_service.safe_filename(title)}.txt",
        title=title,
        category=(str(data.get("category") or "").strip() or None),
    )
    return _result_response(result)


@knowledge_bp.route('/api/knowledge/sync-faq', methods=['POST'])
def sync_faq():
    return _result_response(ingest_service.sync_faqs(force=_truthy(request.args.get("force"))))


@knowledge_bp.route('/api/knowledge', methods=['GET'])
def list_documents():
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "limit/offset phải là số nguyên"}), 400

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            documents = repository.list_documents(cursor, limit=limit, offset=offset)
    except Exception:  # noqa: BLE001
        logger.exception("[KnowledgeAPI] Lỗi liệt kê tài liệu")
        return jsonify({"error": "Lỗi hệ thống khi đọc danh sách tài liệu."}), 500
    finally:
        connection.close()
    return jsonify({
        "status": "success",
        "count": len(documents),
        "documents": [_serialize_document(d) for d in documents],
    })


@knowledge_bp.route('/api/knowledge/<int:document_id>', methods=['GET'])
def get_document(document_id):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            document = repository.get_document(cursor, document_id)
    finally:
        connection.close()
    if not document:
        return jsonify({"error": "Không tìm thấy tài liệu"}), 404
    return jsonify({"status": "success", "document": _serialize_document(document)})


@knowledge_bp.route('/api/knowledge/<int:document_id>', methods=['PATCH'])
def update_document(document_id):
    data, error = _parse_id_json()
    if error:
        return error
    if not isinstance(data.get("is_active"), bool):
        return jsonify({"error": "Cần trường 'is_active' kiểu boolean."}), 400
    if not ingest_service.set_document_active(document_id, data["is_active"]):
        return jsonify({"error": "Không tìm thấy tài liệu hoặc không có thay đổi"}), 404
    return jsonify({"status": "success", "document_id": document_id,
                    "is_active": data["is_active"]})


@knowledge_bp.route('/api/knowledge/<int:document_id>/reindex', methods=['POST'])
def reindex_document(document_id):
    return _result_response(ingest_service.reindex_document(document_id))


@knowledge_bp.route('/api/knowledge/<int:document_id>', methods=['DELETE'])
def delete_document(document_id):
    if not ingest_service.delete_document(document_id):
        return jsonify({"error": "Không tìm thấy tài liệu"}), 404
    return jsonify({"status": "success", "document_id": document_id, "deleted": True})


@knowledge_bp.route('/api/knowledge/search', methods=['POST'])
def search_knowledge():
    """Xem trực tiếp các đoạn tri thức mà RAG sẽ lấy cho 1 câu hỏi (kèm điểm),
    dùng để hiệu chỉnh RAG_MIN_SIMILARITY. Chỉ admin mới xem được nội dung chunk."""
    data, error = _parse_id_json()
    if error:
        return error
    query = str(data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Thiếu 'query'."}), 400
    try:
        top_k = min(max(int(data.get("top_k", 5)), 1), 20)
        min_score = float(data.get("min_score", 0.0))
    except (TypeError, ValueError):
        return jsonify({"error": "top_k phải là số nguyên, min_score là số thực."}), 400

    from knowledge.retriever import get_retriever
    try:
        results = get_retriever().search(query, top_k=top_k, min_score=min_score)
    except Exception:  # noqa: BLE001
        logger.exception("[KnowledgeAPI] Lỗi tìm kiếm KB")
        return jsonify({"error": "Lỗi hệ thống khi truy vấn Knowledge Base."}), 500
    return jsonify({
        "status": "success",
        "query": query,
        "results": [dict(item.to_dict(), citation=item.citation()) for item in results],
    })
