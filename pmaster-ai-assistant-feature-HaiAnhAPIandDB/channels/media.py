"""
Tải ảnh người dùng gửi qua Messenger/Zalo (URL CDN của nền tảng) để AI đọc (D1-02).

An toàn:
- Chỉ nhận URL https (URL nằm trong webhook ĐÃ xác thực chữ ký).
- Đọc dạng stream, dừng ngay khi vượt MAX_IMAGE_SIZE_MB -> không tải file khổng lồ.
- Kiểm tra lại nội dung THẬT là ảnh JPG/PNG/WEBP bằng image_handler.validate_image
  (không tin Content-Type do máy chủ ngoài trả về).
"""
from __future__ import annotations

import io
import logging
from urllib.parse import urlparse

import httpx
from werkzeug.datastructures import FileStorage

from channels.base import http_client
from config import MAX_IMAGE_SIZE_MB
from image_handler import validate_image

logger = logging.getLogger("pmaster.channels.media")

MAX_BYTES = MAX_IMAGE_SIZE_MB * 1024 * 1024
EXT_BY_CONTENT_TYPE = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class MediaError(Exception):
    """Ảnh không tải được hoặc không hợp lệ - thông điệp an toàn để báo người dùng."""


def download_image(url: str) -> dict:
    """Trả về image_data như image_handler.validate_image: {bytes, mime_type, ext}."""
    if urlparse(url).scheme != "https":
        raise MediaError("Đường dẫn ảnh không hợp lệ.")

    try:
        with http_client().stream("GET", url, follow_redirects=True) as response:
            if response.status_code != 200:
                raise MediaError("Không tải được ảnh từ nền tảng.")
            chunks, total = [], 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise MediaError(f"Ảnh vượt quá giới hạn {MAX_IMAGE_SIZE_MB}MB.")
                chunks.append(chunk)
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    except httpx.HTTPError as exc:
        logger.warning("[Media] Lỗi tải ảnh: %s", type(exc).__name__)
        raise MediaError("Không tải được ảnh, vui lòng gửi lại.") from exc

    raw = b"".join(chunks)
    # validate_image cần tên file có đuôi hợp lệ; đuôi thật do Pillow xác định lại.
    ext = EXT_BY_CONTENT_TYPE.get(content_type, "jpg")
    is_valid, error_message, image_data = validate_image(
        FileStorage(stream=io.BytesIO(raw), filename=f"channel_image.{ext}")
    )
    if not is_valid:
        raise MediaError(error_message)
    return image_data
