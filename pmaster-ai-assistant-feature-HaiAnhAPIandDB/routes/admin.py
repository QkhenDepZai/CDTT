"""
API cho Quản trị nghiệp vụ - yêu cầu header X-Admin-Key (dùng bởi trang /admin).

FAQ (D1-11: "nhập câu hỏi - câu trả lời trực tiếp", có hiệu lực ngay):
    GET    /api/admin/faqs                 ?include_inactive=true
    POST   /api/admin/faqs                 {intent, cau_hoi_mau, tra_loi_chuan, nhom_nghiep_vu?}
    PUT    /api/admin/faqs/<id>            các trường cần sửa
    DELETE /api/admin/faqs/<id>            ẩn FAQ (is_active = 0, giữ lịch sử)
Mọi thay đổi được đồng bộ ngay sang Knowledge Base (ingest_service.sync_faqs);
bộ so khớp FAQ trực tiếp đọc bảng faqs nên cũng thấy dữ liệu mới ngay.

Báo cáo (D1-12):
    GET /api/admin/reports/summary          ?period=day|week|month&from=YYYY-MM-DD&to=YYYY-MM-DD
    GET /api/admin/reports/unanswered.xlsx  ?from=&to=
"""
import logging

from flask import Blueprint, Response, jsonify, request

import reports
from database import create_faq, get_db_connection, get_faq, list_faqs, update_faq
from knowledge import ingest_service
from security import require_admin_key

logger = logging.getLogger("pmaster.routes.admin")

admin_bp = Blueprint('admin', __name__)

FAQ_LIMITS = {"nhom_nghiep_vu": 100, "intent": 150, "cau_hoi_mau": 5000, "tra_loi_chuan": 10000}
FAQ_REQUIRED = ("intent", "cau_hoi_mau", "tra_loi_chuan")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@admin_bp.before_request
def _check_admin():
    if request.method == "OPTIONS":
        return None
    return require_admin_key()


def _server_error(context):
    logger.exception("[Admin] Lỗi xử lý %s", context)
    return jsonify({"error": "Lỗi hệ thống, vui lòng thử lại sau."}), 500


def _clean_faq_fields(data, partial):
    """Trả về (fields, None) hoặc (None, thông báo lỗi)."""
    fields = {}
    for name, limit in FAQ_LIMITS.items():
        if name not in data:
            continue
        value = str(data.get(name) or "").strip()
        if len(value) > limit:
            return None, f"'{name}' tối đa {limit} ký tự."
        fields[name] = value or None
    if "is_active" in data:
        fields["is_active"] = 1 if data["is_active"] in (True, 1, "1", "true") else 0
    required = [name for name in FAQ_REQUIRED if name in fields] if partial else FAQ_REQUIRED
    missing = [name for name in required if not fields.get(name)]
    if missing:
        return None, f"Thiếu hoặc rỗng: {', '.join(missing)}."
    if not fields:
        return None, "Không có trường nào để cập nhật."
    return fields, None


def _sync_knowledge_base():
    """Đồng bộ FAQ -> RAG. Lỗi embedding không làm mất thay đổi đã lưu: FAQ
    matcher vẫn dùng được ngay, chỉ cần bấm đồng bộ lại khi Gemini ổn định."""
    try:
        return ingest_service.sync_faqs().to_dict()
    except Exception:  # noqa: BLE001
        logger.exception("[Admin] Đồng bộ FAQ sang Knowledge Base lỗi")
        return {"status": "failed", "message": "Đồng bộ Knowledge Base lỗi, thử lại sau."}


# ---- FAQ ------------------------------------------------------------------------------------
@admin_bp.route('/api/admin/faqs', methods=['GET'])
def admin_list_faqs():
    include_inactive = request.args.get("include_inactive", "").lower() in ("1", "true", "yes")
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            faqs = list_faqs(cursor, include_inactive)
        return jsonify({"status": "success", "count": len(faqs), "faqs": faqs})
    except Exception:  # noqa: BLE001
        return _server_error("admin_list_faqs")
    finally:
        connection.close()


@admin_bp.route('/api/admin/faqs', methods=['POST'])
def admin_create_faq():
    fields, error = _clean_faq_fields(request.get_json(silent=True) or {}, partial=False)
    if error:
        return jsonify({"error": error}), 400
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            faq_id = create_faq(cursor, connection, fields)
            faq = get_faq(cursor, faq_id)
    except Exception:  # noqa: BLE001
        return _server_error("admin_create_faq")
    finally:
        connection.close()
    return jsonify({"status": "success", "faq": faq, "knowledge_sync": _sync_knowledge_base()}), 201


@admin_bp.route('/api/admin/faqs/<int:faq_id>', methods=['PUT'])
def admin_update_faq(faq_id):
    fields, error = _clean_faq_fields(request.get_json(silent=True) or {}, partial=True)
    if error:
        return jsonify({"error": error}), 400
    return _apply_faq_change(faq_id, fields)


@admin_bp.route('/api/admin/faqs/<int:faq_id>', methods=['DELETE'])
def admin_delete_faq(faq_id):
    return _apply_faq_change(faq_id, {"is_active": 0})


def _apply_faq_change(faq_id, fields):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            if not get_faq(cursor, faq_id):
                return jsonify({"error": "Không tìm thấy FAQ"}), 404
            update_faq(cursor, connection, faq_id, fields)
            faq = get_faq(cursor, faq_id)
    except Exception:  # noqa: BLE001
        return _server_error("admin_update_faq")
    finally:
        connection.close()
    return jsonify({"status": "success", "faq": faq, "knowledge_sync": _sync_knowledge_base()})


# ---- Báo cáo --------------------------------------------------------------------------------
def _report_range():
    try:
        return reports.parse_range(request.args.get("from"), request.args.get("to")), None
    except reports.ReportRangeError as exc:
        return None, (jsonify({"error": str(exc)}), 400)


@admin_bp.route('/api/admin/reports/summary', methods=['GET'])
def admin_report_summary():
    period = request.args.get("period", "day")
    if period not in reports.PERIODS:
        return jsonify({"error": "period phải là day, week hoặc month"}), 400
    date_range, error = _report_range()
    if error:
        return error
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            data = reports.summary(cursor, period, *date_range)
        return jsonify({"status": "success", **data})
    except Exception:  # noqa: BLE001
        return _server_error("admin_report_summary")
    finally:
        connection.close()


@admin_bp.route('/api/admin/reports/unanswered.xlsx', methods=['GET'])
def admin_report_unanswered():
    date_range, error = _report_range()
    if error:
        return error
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            content = reports.build_unanswered_workbook(
                reports.fetch_unanswered(cursor, *date_range),
                reports.fetch_handovers(cursor, *date_range),
            )
    except Exception:  # noqa: BLE001
        return _server_error("admin_report_unanswered")
    finally:
        connection.close()
    date_from, date_to = date_range
    filename = f"cau_hoi_chua_tra_loi_{date_from:%Y%m%d}_{date_to:%Y%m%d}.xlsx"
    return Response(content, mimetype=XLSX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
