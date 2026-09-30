"""Tiện ích chuẩn hoá văn bản tiếng Việt dùng chung cho các bộ so khớp luật."""
import re
import unicodedata


def normalize_vi(text: str) -> str:
    """Chữ thường, dạng Unicode NFC, gộp khoảng trắng (giữ nguyên dấu)."""
    text = unicodedata.normalize("NFC", str(text or "")).lower()
    return re.sub(r"\s+", " ", text).strip()


def strip_accents(text: str) -> str:
    """'Địa ĐIỂM thi Ở ĐÂU?' -> 'dia diem thi o dau?' (chuẩn hoá để so khớp)."""
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text.replace("đ", "d")).strip()
