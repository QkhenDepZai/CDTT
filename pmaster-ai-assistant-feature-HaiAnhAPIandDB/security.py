"""Xác thực request: site_key, user_token (HMAC), khoá API quản trị.

Kiểm soát NỘI DUNG tin nhắn (prompt injection, từ khoá cấm...) nằm ở guardrails.py.
"""
import base64
import hashlib
import hmac

from flask import jsonify, request


def get_request_site_key():
    """Đọc site_key từ JSON/form hoặc header X-Site-Key."""
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form or {}
    return data.get("site_key") or request.headers.get("X-Site-Key")


def require_staff_role(get_db_connection, get_user_role):
    """Decorator-like helper cho route staff; trả (error_response, staff_id)."""
    data = request.get_json(silent=True) or {}
    staff_id = data.get("staff_id") or request.headers.get("X-Staff-Id")
    if not staff_id:
        return jsonify({"error": "Thiếu staff_id"}), None

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            role = get_user_role(cursor, staff_id)
        if role != "staff":
            return jsonify({"error": "Tài khoản không có quyền tư vấn viên"}), None
    finally:
        connection.close()

    return None, int(staff_id)


# ============================================================
# USER TOKEN & ADMIN KEY (Giai đoạn 3)
# ============================================================
def _user_token_secret():
    from config import APP_SECRET_KEY
    return APP_SECRET_KEY.encode("utf-8") if APP_SECRET_KEY else None


def issue_user_token(user_id):
    """Token = HMAC-SHA256(APP_SECRET_KEY, "user:<id>") dạng base64url.
    Không lưu DB (stateless); None nếu chưa cấu hình APP_SECRET_KEY."""
    secret = _user_token_secret()
    if secret is None or user_id is None:
        return None
    digest = hmac.new(secret, f"user:{int(user_id)}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_user_token(user_id, token):
    expected = issue_user_token(user_id)
    if not expected or not token:
        return False
    # compare_digest: so sánh thời gian hằng, chống dò token theo thời gian phản hồi.
    return hmac.compare_digest(expected, str(token))


def get_request_user_token():
    """Đọc user_token từ header X-User-Token (ưu tiên), JSON, form hoặc query."""
    token = request.headers.get("X-User-Token")
    if token:
        return token
    if request.is_json:
        token = (request.get_json(silent=True) or {}).get("user_token")
    else:
        token = request.form.get("user_token")
    return token or request.args.get("user_token")


def require_admin_key():
    """Trả None nếu X-Admin-Key hợp lệ, ngược lại trả (response, status)."""
    from config import ADMIN_API_KEY
    if not ADMIN_API_KEY:
        return jsonify({"error": "API quản trị chưa được bật (thiếu ADMIN_API_KEY trên server)."}), 503
    provided = request.headers.get("X-Admin-Key", "")
    if not hmac.compare_digest(provided.encode("utf-8"), ADMIN_API_KEY.encode("utf-8")):
        return jsonify({"error": "Sai hoặc thiếu X-Admin-Key."}), 401
    return None
