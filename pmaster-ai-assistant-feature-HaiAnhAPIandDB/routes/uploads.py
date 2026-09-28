from flask import Blueprint, send_from_directory
from config import UPLOAD_FOLDER

uploads_bp = Blueprint('uploads', __name__)


@uploads_bp.route('/uploads/images/<path:filename>', methods=['GET'])
def serve_image(filename):
    """Phục vụ lại ảnh đã upload để Widget/Staff Dashboard hiển thị."""
    return send_from_directory(UPLOAD_FOLDER, filename)
