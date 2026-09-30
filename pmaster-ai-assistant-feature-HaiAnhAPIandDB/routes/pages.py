"""
Phục vụ giao diện tĩnh trong thư mục frontend/ từ chính backend:

    /widget.js   script nhúng chatbot vào website bất kỳ (1 dòng <script>)
    /demo        trang website mẫu đã nhúng widget để test nhanh
    /staff       Staff Dashboard cho tư vấn viên (D1-09)
    /admin       trang quản trị FAQ / tài liệu / báo cáo (D1-11, D1-12)
    /assets/...  logo, icon dùng chung
"""
import os

from flask import Blueprint, send_from_directory

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

pages_bp = Blueprint('pages', __name__)


def _page(filename):
    response = send_from_directory(FRONTEND_DIR, filename, max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@pages_bp.route('/widget.js', methods=['GET'])
def widget_script():
    response = send_from_directory(FRONTEND_DIR, "widget.js", mimetype="application/javascript",
                                   max_age=300)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@pages_bp.route('/demo', methods=['GET'])
def demo_page():
    return _page("demo.html")


@pages_bp.route('/staff', methods=['GET'])
def staff_page():
    return _page("staff.html")


@pages_bp.route('/admin', methods=['GET'])
def admin_page():
    return _page("admin.html")


@pages_bp.route('/assets/<path:filename>', methods=['GET'])
def assets(filename):
    return send_from_directory(os.path.join(FRONTEND_DIR, "assets"), filename, max_age=86400)
