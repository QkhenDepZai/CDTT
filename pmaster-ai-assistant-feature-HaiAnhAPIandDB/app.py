import logging

from flask import Flask, jsonify
from cors import handle_cors_and_site_key, add_cors_headers
from routes.chat import chat_bp
from routes.staff import staff_bp
from routes.uploads import uploads_bp
from routes.health import health_bp
from config import MAX_IMAGE_SIZE_MB

# Log rõ ràng ra terminal (model, attempt, status_code, error_type...) thay vì
# print() rải rác - xem gemini_client.py / moderation.py / conversation_memory.py.
# KHÔNG log API key hay nội dung tin nhắn người dùng (xem docstring các module đó).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = Flask(__name__)

# Chặn sớm ở tầng Flask nếu request quá lớn (thêm buffer nhỏ cho phần form-data khác
# ngoài file ảnh); thông báo lỗi "Dung lượng ảnh vượt quá giới hạn 5MB" cụ thể hơn
# vẫn do image_handler.validate_image() xử lý khi request lọt qua được tầng này.
app.config['MAX_CONTENT_LENGTH'] = (MAX_IMAGE_SIZE_MB + 1) * 1024 * 1024

app.before_request(handle_cors_and_site_key)
app.after_request(add_cors_headers)

app.register_blueprint(chat_bp)
app.register_blueprint(staff_bp)
app.register_blueprint(uploads_bp)
app.register_blueprint(health_bp)


@app.errorhandler(413)
def request_entity_too_large(_error):
    return jsonify({
        "error": "Dung lượng request vượt quá giới hạn cho phép.",
        "max_image_size_mb": MAX_IMAGE_SIZE_MB,
    }), 413


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)
