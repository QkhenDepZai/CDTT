from urllib.parse import urlparse
from flask import request, jsonify
from database import get_site_by_key
from security import get_request_site_key


def _origin_matches(origin: str, allowed_domain: str) -> bool:
    if not origin or allowed_domain == "*":
        return True
    parsed = urlparse(origin)
    origin_host = parsed.netloc.lower()
    allowed = allowed_domain.lower().strip()
    if "://" in allowed:
        allowed = urlparse(allowed).netloc.lower()
    else:
        allowed = allowed.split("/")[0].lower()
    return origin_host == allowed


def handle_cors_and_site_key():
    if request.method == "OPTIONS":
        return

    if request.path.startswith("/api/chat"):
        site_key = get_request_site_key()
        if not site_key:
            return jsonify({"error": "Thiếu site_key"}), 401

        site = get_site_by_key(site_key)
        if not site or not site["is_active"]:
            return jsonify({"error": "site_key không hợp lệ"}), 401

        origin = request.headers.get("Origin", "")
        if origin and not _origin_matches(origin, site["allowed_domain"]):
            return jsonify({"error": "Domain không được phép"}), 403

        request.site_id = site["id"]
        request.site_key = site_key


def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Site-Key, X-Staff-Id"
    return response
