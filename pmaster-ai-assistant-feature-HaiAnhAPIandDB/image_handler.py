import os
import io
import uuid

from PIL import Image

from config import UPLOAD_FOLDER, MAX_IMAGE_SIZE_MB

# A. VALIDATION & GIỚI HẠN (theo QC_D1_TEST_CASE)
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
ALLOWED_MIME_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_IMAGE_SIZE_BYTES = MAX_IMAGE_SIZE_MB * 1024 * 1024

ERROR_UNSUPPORTED_FORMAT = "Định dạng file không được hỗ trợ"
ERROR_TOO_LARGE = f"Dung lượng ảnh vượt quá giới hạn {MAX_IMAGE_SIZE_MB}MB"


def validate_image(file_storage):
    """
    Kiểm tra 1 file ảnh upload theo đúng yêu cầu QC:
    - Chỉ cho phép JPG, PNG, WEBP (kiểm tra cả đuôi file lẫn nội dung thật của file,
      để tránh trường hợp đổi đuôi .exe thành .png).
    - Dung lượng <= 5MB.

    Trả về (is_valid: bool, error_message: str | None, image_data: dict | None)
    image_data = {"bytes": <raw bytes>, "mime_type": "image/png", "ext": "png"}
    """
    filename = file_storage.filename or ""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        return False, ERROR_UNSUPPORTED_FORMAT, None

    # Kiểm tra dung lượng trước khi đọc hết file vào bộ nhớ
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)

    if size == 0:
        return False, ERROR_UNSUPPORTED_FORMAT, None
    if size > MAX_IMAGE_SIZE_BYTES:
        return False, ERROR_TOO_LARGE, None

    raw = file_storage.read()
    file_storage.stream.seek(0)

    # Xác thực nội dung file thật sự là ảnh hợp lệ (không chỉ dựa vào đuôi file)
    try:
        img = Image.open(io.BytesIO(raw))
        detected_format = img.format  # 'JPEG', 'PNG', 'WEBP'
        img.verify()
    except Exception:
        return False, ERROR_UNSUPPORTED_FORMAT, None

    mime_type = Image.MIME.get(detected_format)
    if mime_type not in ALLOWED_MIME_TYPES:
        return False, ERROR_UNSUPPORTED_FORMAT, None

    detected_ext = {
        "JPEG": "jpg",
        "PNG": "png",
        "WEBP": "webp",
    }.get(detected_format)
    if not detected_ext:
        return False, ERROR_UNSUPPORTED_FORMAT, None

    return True, None, {
        "bytes": raw,
        "mime_type": mime_type,
        "ext": detected_ext,
    }


def save_image(image_bytes, ext):
    """
    Lưu ảnh vào thư mục local UPLOAD_FOLDER (uploads/images/ mặc định).

    Muốn chuyển sang Cloud Storage (S3/Cloudinary) sau này: giữ nguyên chữ ký hàm
    save_image(image_bytes, ext) -> (relative_path_or_key, public_url), chỉ đổi
    phần thân hàm để upload lên cloud và trả về URL công khai của cloud thay vì path local.
    """
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    absolute_path = os.path.join(UPLOAD_FOLDER, filename)

    with open(absolute_path, 'wb') as f:
        f.write(image_bytes)

    relative_path = f"{UPLOAD_FOLDER}/{filename}".replace("\\", "/")
    return relative_path, absolute_path


# Kích thước cạnh dài tối đa (px) khi gửi ảnh cho Gemini để phân tích.
# Ảnh chụp màn hình/điện thoại hiện đại thường 3000-4000px cạnh dài trong khi
# model không cần độ phân giải đó để đọc đề bài/hiểu nội dung -> resize giúp
# giảm token (ảnh được tính token theo số "tile" ảnh, ảnh càng lớn càng nhiều
# tile) và giảm thời gian upload/latency, mà không ảnh hưởng khả năng đọc
# nội dung của model với hầu hết use-case (đề bài, thông báo lỗi, giao diện...).
GEMINI_IMAGE_MAX_DIMENSION = 1280
GEMINI_IMAGE_JPEG_QUALITY = 85


def prepare_image_for_gemini(image_bytes, mime_type):
    """Resize ảnh (nếu cần) trước khi gửi cho Gemini. Ảnh GỐC (chưa resize) vẫn
    được lưu lại trên server qua save_image() để staff xem đúng ảnh thí sinh
    gửi; hàm này chỉ tạo ra 1 bản sao nhỏ hơn CHỈ để gửi Gemini.

    Trả về (bytes, mime_type). Nếu resize thất bại vì bất kỳ lý do gì, trả về
    nguyên ảnh gốc để không chặn luồng chat chính.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        width, height = img.size
        longest_side = max(width, height)

        if longest_side <= GEMINI_IMAGE_MAX_DIMENSION:
            return image_bytes, mime_type

        scale = GEMINI_IMAGE_MAX_DIMENSION / float(longest_side)
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
        img = img.resize(new_size, Image.LANCZOS)

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=GEMINI_IMAGE_JPEG_QUALITY)
        return buffer.getvalue(), "image/jpeg"
    except Exception:
        # Không để lỗi resize làm gián đoạn việc gửi ảnh - fallback về ảnh gốc.
        return image_bytes, mime_type


def build_image_url(relative_path):
    """
    Xây đường dẫn public để Widget/Staff Dashboard render lại ảnh.
    Ứng với route GET /uploads/images/<filename> đăng ký trong routes/uploads.py.
    """
    path = relative_path.replace("\\", "/")
    if not path.startswith("uploads/"):
        path = f"uploads/{path}"
    return f"/{path}"
