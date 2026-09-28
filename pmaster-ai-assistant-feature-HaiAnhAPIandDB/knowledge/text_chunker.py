"""
Cắt văn bản thành chunk cho RAG theo kiểu "recursive character splitting".

Chiến lược:
1. Tách theo ranh giới có nghĩa nhất trước (đoạn văn -> dòng -> câu -> mệnh
   đề -> từ), chỉ xuống cấp nhỏ hơn khi 1 mảnh vẫn dài hơn chunk_size.
2. Gộp các mảnh liền kề thành chunk gần chunk_size nhất.
3. Chunk sau lặp lại phần đuôi (<= chunk_overlap ký tự) của chunk trước để
   câu hỏi rơi đúng ranh giới 2 chunk vẫn tìm được đủ ngữ cảnh.

Không phụ thuộc thư viện ngoài (LangChain...) để dễ kiểm soát hành vi và
hiệu năng; đo bằng số KÝ TỰ (ổn định với tiếng Việt, không cần tokenizer).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from knowledge.document_loader import DocumentSection

# Ranh giới tách, xếp theo mức ưu tiên giảm dần. Dấu câu được giữ lại ở
# cuối mảnh bên trái (xem _split_keep_separator).
SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ": ", ", ", " ")

# Tiếng Việt trung bình ~3.5 ký tự / token với tokenizer của Gemini. Chỉ là
# ước lượng để thống kê & kiểm soát ngân sách context, không dùng để cắt.
CHARS_PER_TOKEN_ESTIMATE = 3.5


@dataclass
class Chunk:
    index: int
    content: str
    page_number: int | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def char_count(self) -> int:
        return len(self.content)

    @property
    def token_estimate(self) -> int:
        return max(1, round(len(self.content) / CHARS_PER_TOKEN_ESTIMATE))


def _split_keep_separator(text: str, separator: str) -> list[str]:
    parts = text.split(separator)
    pieces = [part + separator for part in parts[:-1]]
    pieces.append(parts[-1])
    return [piece for piece in pieces if piece]


def _split_recursive(text: str, chunk_size: int, separators: tuple[str, ...]) -> list[str]:
    """Trả về các mảnh nguyên tử, mỗi mảnh <= chunk_size ký tự."""
    if len(text) <= chunk_size:
        return [text]

    for position, separator in enumerate(separators):
        if separator in text:
            pieces = []
            for piece in _split_keep_separator(text, separator):
                if len(piece) <= chunk_size:
                    pieces.append(piece)
                else:
                    pieces.extend(_split_recursive(piece, chunk_size, separators[position + 1:]))
            return pieces

    # Không còn ranh giới nào (chuỗi liền mạch, ví dụ base64/URL dài): cắt cứng.
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


def _merge_pieces(pieces: list[str], chunk_size: int, chunk_overlap: int) -> list[str]:
    chunks: list[str] = []
    window: list[str] = []
    window_len = 0

    for piece in pieces:
        if window and window_len + len(piece) > chunk_size:
            chunks.append("".join(window).strip())
            # Giữ lại các mảnh cuối làm overlap cho chunk kế tiếp, nhưng luôn
            # chừa chỗ cho mảnh mới để chunk không vượt chunk_size.
            while window and (
                window_len > chunk_overlap or window_len + len(piece) > chunk_size
            ):
                window_len -= len(window[0])
                window.pop(0)
        window.append(piece)
        window_len += len(piece)

    # Sau mỗi lần xả chunk, mảnh mới luôn được thêm vào window, nên phần còn
    # lại luôn chứa nội dung chưa xuất hiện ở chunk trước.
    if window:
        chunks.append("".join(window).strip())
    return [chunk for chunk in chunks if chunk]


def split_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size phải > 0")
    if not 0 <= chunk_overlap < chunk_size:
        raise ValueError("chunk_overlap phải trong khoảng [0, chunk_size)")

    text = (text or "").strip()
    if not text:
        return []
    pieces = _split_recursive(text, chunk_size, SEPARATORS)
    return _merge_pieces(pieces, chunk_size, chunk_overlap)


def chunk_sections(
    sections: list[DocumentSection],
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Cắt từng section thành chunk; giữ page_number/metadata của section gốc.

    Không gộp chunk xuyên qua 2 section (2 trang PDF / 2 dòng CSV) để trích
    dẫn nguồn (trang, dòng) luôn chính xác.
    """
    chunks: list[Chunk] = []
    for section in sections:
        for content in split_text(section.text, chunk_size, chunk_overlap):
            chunks.append(Chunk(
                index=len(chunks),
                content=content,
                page_number=section.page_number,
                metadata=dict(section.metadata),
            ))
    return chunks
