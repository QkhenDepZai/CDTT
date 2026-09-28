"""
Trích xuất văn bản từ tài liệu doanh nghiệp: PDF, TXT, MD, CSV, DOCX.

Mỗi loader trả về list[DocumentSection]. 1 section là 1 đơn vị có ý nghĩa
tự nhiên của tài liệu (1 trang PDF, 1 dòng CSV, toàn bộ file TXT...);
text_chunker sẽ cắt tiếp các section dài thành chunk.

Chuẩn hoá Unicode NFC là BẮT BUỘC với tiếng Việt: PDF xuất từ Word/Mac
thường chứa dấu ở dạng tổ hợp (NFD, "e" + dấu mũ + dấu sắc tách rời), nếu
không chuẩn hoá thì cùng 1 chữ "thể lệ" sẽ ra embedding/từ khoá khác nhau.
"""
from __future__ import annotations

import codecs
import csv
import io
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field

logger = logging.getLogger("pmaster.knowledge.loader")

SUPPORTED_EXTENSIONS = {
    "pdf": "pdf",
    "txt": "txt",
    "md": "md",
    "csv": "csv",
    "docx": "docx",
}

MIME_TYPES = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Thứ tự thử giải mã file text (sau khi đã xét BOM UTF-16 của Excel
# "Unicode Text"): UTF-8 (có/không BOM) -> Windows-1258 (bảng mã tiếng Việt
# của Windows/Excel cũ).
TEXT_ENCODINGS = ("utf-8-sig", "cp1258")

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_SPACES = re.compile(r"[ \t ]+")
_MULTI_NEWLINES = re.compile(r"\n{3,}")
# Từ bị ngắt dòng bằng gạch nối trong PDF: "đăng-\nký" -> "đăngký" là sai với
# tiếng Việt (có khoảng trắng giữa các âm tiết), nên chỉ nối lại cho chữ Latin
# không dấu (tên hàm, thuật ngữ tiếng Anh).
_HYPHEN_BREAK = re.compile(r"([A-Za-z])-\n([a-z])")


class DocumentLoadError(Exception):
    """Lỗi đọc/trích xuất tài liệu - thông điệp an toàn để trả cho người dùng."""


@dataclass
class DocumentSection:
    text: str
    page_number: int | None = None
    metadata: dict = field(default_factory=dict)


def detect_source_type(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        raise DocumentLoadError(
            f"Định dạng '.{ext}' chưa được hỗ trợ. "
            f"Chỉ chấp nhận: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )
    return SUPPORTED_EXTENSIONS[ext]


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _MULTI_SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _MULTI_NEWLINES.sub("\n\n", text)
    return text.strip()


def decode_text_bytes(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise DocumentLoadError("File UTF-16 bị lỗi mã hoá.") from exc
    for encoding in TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentLoadError(
        "Không giải mã được file văn bản. Hãy lưu lại file với bảng mã UTF-8."
    )


def _load_pdf(raw: bytes) -> list[DocumentSection]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            # Thử mật khẩu rỗng (PDF chỉ khoá quyền in/sửa, không khoá đọc).
            if not reader.decrypt(""):
                raise DocumentLoadError("PDF được bảo vệ bằng mật khẩu, không thể đọc.")
        sections = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_text = normalize_text(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001 - 1 trang lỗi không làm hỏng cả file
                logger.warning("[Loader] Bỏ qua trang %s do lỗi trích xuất: %s", page_number, exc)
                continue
            if page_text:
                sections.append(DocumentSection(text=page_text, page_number=page_number))
    except DocumentLoadError:
        raise
    except PdfReadError as exc:
        raise DocumentLoadError(f"File PDF bị hỏng hoặc không hợp lệ: {exc}") from exc

    if not sections:
        raise DocumentLoadError(
            "PDF không có lớp văn bản (có thể là bản scan ảnh). "
            "Cần OCR hoặc cung cấp bản PDF xuất trực tiếp từ Word."
        )
    return sections


def _load_plain_text(raw: bytes) -> list[DocumentSection]:
    text = normalize_text(decode_text_bytes(raw))
    return [DocumentSection(text=text)] if text else []


def _load_csv(raw: bytes) -> list[DocumentSection]:
    """Mỗi dòng CSV -> 1 section dạng "Cột A: giá trị | Cột B: giá trị".

    Ghép tên cột vào từng dòng để chunk tự mang đủ ngữ cảnh khi được truy
    xuất riêng lẻ (ví dụ "Bảng thi: A | Lệ phí: ..." thay vì chỉ "A | ...").
    """
    text = decode_text_bytes(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = [normalize_text(col) for col in next(reader)]
    except StopIteration:
        return []

    if not any(header):
        raise DocumentLoadError("File CSV thiếu dòng tiêu đề (header).")

    sections = []
    for row_number, row in enumerate(reader, start=2):
        pairs = []
        for index, value in enumerate(row):
            value = normalize_text(value)
            if not value:
                continue
            column = header[index] if index < len(header) and header[index] else f"Cột {index + 1}"
            pairs.append(f"{column}: {value}")
        if pairs:
            sections.append(DocumentSection(
                text=" | ".join(pairs),
                metadata={"row_number": row_number},
            ))
    return sections


def _load_docx(raw: bytes) -> list[DocumentSection]:
    """Đọc DOCX theo đúng thứ tự xuất hiện của đoạn văn và bảng."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = Document(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 - python-docx ném nhiều loại lỗi khác nhau
        raise DocumentLoadError(f"File DOCX bị hỏng hoặc không hợp lệ: {exc}") from exc

    lines = []
    for block in document.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(block, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = (paragraph.style.name or "").lower() if paragraph.style else ""
            # Giữ tiêu đề thành đoạn riêng để chunker ưu tiên cắt tại đây.
            lines.append(f"\n{text}\n" if style.startswith("heading") else text)
        elif tag == "tbl":
            table = Table(block, document)
            for row in table.rows:
                cells = []
                for cell in row.cells:
                    cell_text = cell.text.strip()
                    # Ô gộp (merged) được python-docx trả lặp lại -> bỏ trùng liền kề.
                    if cell_text and (not cells or cells[-1] != cell_text):
                        cells.append(cell_text)
                if cells:
                    lines.append(" | ".join(cells))
            lines.append("")

    text = normalize_text("\n".join(lines))
    return [DocumentSection(text=text)] if text else []


_LOADERS = {
    "pdf": _load_pdf,
    "txt": _load_plain_text,
    "md": _load_plain_text,
    "csv": _load_csv,
    "docx": _load_docx,
}


def load_document_bytes(raw: bytes, filename: str) -> list[DocumentSection]:
    """Trích xuất text từ nội dung file. Raise DocumentLoadError nếu thất bại."""
    source_type = detect_source_type(filename)
    if not raw:
        raise DocumentLoadError("File rỗng.")

    sections = _LOADERS[source_type](raw)
    sections = [s for s in sections if s.text.strip()]
    if not sections:
        raise DocumentLoadError("Không trích xuất được nội dung văn bản nào từ file.")

    total_chars = sum(len(s.text) for s in sections)
    logger.info(
        "[Loader] %s: type=%s sections=%s chars=%s",
        os.path.basename(filename), source_type, len(sections), total_chars,
    )
    return sections


def load_document(path: str) -> list[DocumentSection]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise DocumentLoadError(f"Không đọc được file '{path}': {exc}") from exc
    return load_document_bytes(raw, path)
