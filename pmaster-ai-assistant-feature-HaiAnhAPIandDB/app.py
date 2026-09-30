import logging
from datetime import date, datetime

from flask import Flask, jsonify, request
from flask.json.provider import DefaultJSONProvider
from cors import handle_cors_and_site_key, add_cors_headers
from routes.chat import chat_bp
from routes.staff import staff_bp
from routes.uploads import uploads_bp
from routes.health import health_bp
from routes.history import history_bp
from routes.knowledge import knowledge_bp
from routes.webhooks import webhooks_bp
from routes.admin import admin_bp
from routes.pages import pages_bp
from config import KNOWLEDGE_MAX_FILE_MB, MAX_IMAGE_SIZE_MB
from image_handler import ERROR_TOO_LARGE

MB = 1024 * 1024

# Log rõ ràng ra terminal (model, attempt, status_code, error_type...) thay vì
# print() rải rác - xem gemini_client.py / moderation.py / conversation_memory.py.
# KHÔNG log API key hay nội dung tin nhắn người dùng (xem docstring các module đó).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


class JSONProvider(DefaultJSONProvider):
    """Ngày giờ trả dạng "YYYY-MM-DD HH:MM:SS" (giờ lưu trong MySQL) thay vì
    định dạng HTTP "..., GMT" mặc định của Flask - dễ hiểu nhầm múi giờ."""

    @staticmethod
    def default(o):
        if isinstance(o, datetime):
            return o.isoformat(sep=" ")
        if isinstance(o, date):
            return o.isoformat()
        return DefaultJSONProvider.default(o)


app = Flask(__name__)
app.json = JSONProvider(app)

# Giới hạn chung = file lớn nhất được phép (tài liệu Knowledge Base) + buffer
# cho các trường form-data khác. Riêng route ảnh bị chặn chặt hơn ở
# _limit_image_upload_size bên dưới; thông báo "ảnh vượt 5MB" cụ thể hơn vẫn
# do image_handler.validate_image() xử lý.
app.config['MAX_CONTENT_LENGTH'] = (max(MAX_IMAGE_SIZE_MB, KNOWLEDGE_MAX_FILE_MB) + 1) * MB
app.json.ensure_ascii = False  # trả JSON tiếng Việt dễ đọc trên Postman


@app.before_request
def _limit_image_upload_size():
    # Từ chối sớm theo header Content-Length, trước khi Flask đọc body vào bộ nhớ.
    if request.path.startswith("/api/chat/image") and \
            (request.content_length or 0) > (MAX_IMAGE_SIZE_MB + 1) * MB:
        # Cùng mã lỗi/thông báo với image_handler.validate_image (TC-IMG-03).
        return jsonify({"error": ERROR_TOO_LARGE, "max_image_size_mb": MAX_IMAGE_SIZE_MB}), 400
    return None


app.before_request(handle_cors_and_site_key)
app.after_request(add_cors_headers)

app.register_blueprint(chat_bp)
app.register_blueprint(staff_bp)
app.register_blueprint(uploads_bp)
app.register_blueprint(health_bp)
app.register_blueprint(history_bp)
app.register_blueprint(knowledge_bp)
app.register_blueprint(webhooks_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(pages_bp)


@app.errorhandler(413)
def request_entity_too_large(_error):
    return jsonify({
        "error": "Dung lượng request vượt quá giới hạn cho phép.",
        "max_image_size_mb": MAX_IMAGE_SIZE_MB,
        "max_document_size_mb": KNOWLEDGE_MAX_FILE_MB,
    }), 413


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Không tìm thấy endpoint."}), 404


@app.errorhandler(405)
def method_not_allowed(_error):
    return jsonify({"error": "Phương thức HTTP không được hỗ trợ cho endpoint này."}), 405


@app.errorhandler(500)
def internal_error(_error):
    # Không trả chi tiết lỗi (stack trace, câu SQL...) ra ngoài; xem log server.
    return jsonify({"error": "Lỗi hệ thống, vui lòng thử lại sau."}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)
