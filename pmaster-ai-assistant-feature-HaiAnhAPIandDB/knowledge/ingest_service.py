"""
Điều phối nạp tri thức vào Knowledge Base.

    ingest_bytes / ingest_file : nạp 1 tài liệu (PDF, TXT, MD, CSV, DOCX)
    sync_faqs                  : đồng bộ bảng faqs -> 1 tài liệu ảo source_type='faq'
    reindex_document           : trích xuất + embed lại (đổi model, sửa lỗi...)
    set_document_active        : bật/tắt tài liệu khỏi RAG mà không xoá
    delete_document            : xoá hẳn tài liệu + chunk + file đã lưu

Nguyên tắc:
- Chống trùng bằng SHA-256 nội dung file (UNIQUE content_hash).
- Gọi Embedding API (chậm, qua mạng) NGOÀI giao dịch DB; chỉ mở giao dịch
  ngắn khi ghi chunk -> không giữ khoá bảng lâu.
- Ghi chunk + chuyển status='ready' trong CÙNG 1 giao dịch: tài liệu không
  bao giờ ở trạng thái "ready nhưng thiếu chunk".
- Lỗi ở bất kỳ bước nào -> status='failed' + error_message, không raise ra
  ngoài (API upload ở Giai đoạn 3 trả thông báo rõ ràng cho quản trị viên).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import pymysql

from config import (
    CHUNK_OVERLAP_CHARS,
    CHUNK_SIZE_CHARS,
    KNOWLEDGE_MAX_FILE_MB,
    KNOWLEDGE_UPLOAD_FOLDER,
)
from database import get_db_connection
from knowledge import repository
from knowledge.document_loader import (
    MIME_TYPES,
    DocumentLoadError,
    DocumentSection,
    detect_source_type,
    load_document_bytes,
    normalize_text,
)
from knowledge.embedding_service import EmbeddingError, EmbeddingService, get_embedding_service
from knowledge.text_chunker import Chunk, chunk_sections, split_text

logger = logging.getLogger("pmaster.knowledge.ingest")

FAQ_DOCUMENT_TITLE = "Bộ câu hỏi thường gặp (FAQ) Python Master 2026"
# 1 FAQ ngắn hơn ngưỡng này được giữ nguyên thành 1 chunk (câu hỏi + trả lời
# không bị tách rời); dài hơn mới cắt nhỏ.
FAQ_SINGLE_CHUNK_MAX_CHARS = CHUNK_SIZE_CHARS * 3

# Tài liệu kẹt ở 'processing' lâu hơn ngưỡng này (server tắt giữa chừng khi
# đang embed) được coi là hỏng và cho phép xử lý lại khi upload lần nữa.
STALE_PROCESSING_MINUTES = 30

# Xử lý nền cho upload lớn (background=True). 2 luồng: đủ để không chặn
# request khác, không vượt quota phút của Embedding API.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kb-ingest")


@dataclass
class IngestResult:
    status: str  # created | updated | duplicate | unchanged | processing | failed
    message: str
    document_id: int | None = None
    chunk_count: int = 0
    # Chỉ có khi status='failed': validation (file/dữ liệu không hợp lệ) |
    # embedding (Gemini Embedding lỗi/hết quota - thử lại sau) | system (lỗi DB/code).
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != "failed"

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def safe_filename(filename: str) -> str:
    """Tên file an toàn để lưu đĩa: bỏ đường dẫn (chống path traversal),
    bỏ dấu tiếng Việt, chỉ giữ [A-Za-z0-9._-]."""
    name = os.path.basename(filename or "document")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name[:120] or "document"


def _notify_index_changed():
    # Import trễ để tránh vòng import retriever <-> ingest_service.
    from knowledge.retriever import get_retriever
    get_retriever().invalidate()


def _store_file(raw: bytes, filename: str, content_hash: str) -> str:
    os.makedirs(KNOWLEDGE_UPLOAD_FOLDER, exist_ok=True)
    path = os.path.join(KNOWLEDGE_UPLOAD_FOLDER, f"{content_hash[:16]}_{safe_filename(filename)}")
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(raw)
    return path.replace("\\", "/")


def _embed_and_store(document_id: int, title: str, chunks: list[Chunk],
                     embeddings: EmbeddingService) -> int:
    """Embed (ngoài giao dịch) rồi thay toàn bộ chunk của tài liệu (trong giao dịch)."""
    if not chunks:
        raise DocumentLoadError("Tài liệu không có đoạn văn bản nào để lập chỉ mục.")

    vectors = embeddings.embed_documents([chunk.content for chunk in chunks], title=title)

    connection = get_db_connection()
    try:
        connection.begin()
        with connection.cursor() as cursor:
            repository.delete_chunks(cursor, document_id)
            repository.insert_chunks(cursor, document_id, chunks, vectors, embeddings.model)
            repository.mark_ready(cursor, document_id, len(chunks), embeddings.model,
                                  embeddings.dimension)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return len(chunks)


def _mark_failed(document_id: int, error_message: str):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            repository.mark_failed(cursor, document_id, error_message)
        connection.commit()
    except Exception as exc:  # noqa: BLE001 - chỉ ghi log, lỗi gốc đã được báo
        logger.error("[Ingest] Không cập nhật được status failed cho #%s: %s", document_id, exc)
    finally:
        connection.close()


def _process(document_id: int, title: str, sections: list[DocumentSection] | None,
             chunks: list[Chunk] | None, embeddings: EmbeddingService,
             success_status: str) -> IngestResult:
    try:
        if chunks is None:
            chunks = chunk_sections(sections or [], CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
        count = _embed_and_store(document_id, title, chunks, embeddings)
    except (DocumentLoadError, EmbeddingError) as exc:
        logger.warning("[Ingest] Tài liệu #%s thất bại: %s", document_id, exc)
        _mark_failed(document_id, str(exc))
        reason = "embedding" if isinstance(exc, EmbeddingError) else "validation"
        return IngestResult("failed", str(exc), document_id, reason=reason)
    except Exception as exc:  # noqa: BLE001 - mọi lỗi khác cũng phải về status failed
        logger.exception("[Ingest] Lỗi không mong đợi với tài liệu #%s", document_id)
        _mark_failed(document_id, f"Lỗi hệ thống: {type(exc).__name__}")
        return IngestResult("failed", "Lỗi hệ thống khi xử lý tài liệu, xem log server.",
                            document_id, reason="system")

    _notify_index_changed()
    logger.info("[Ingest] Tài liệu #%s '%s' sẵn sàng: %s chunk.", document_id, title, count)
    return IngestResult(success_status, f"Đã lập chỉ mục {count} đoạn tri thức.",
                        document_id, count)


# ---- API công khai -----------------------------------------------------------
def _is_stale(document: dict) -> bool:
    minutes = document.get("minutes_since_update")
    return minutes is not None and minutes >= STALE_PROCESSING_MINUTES


def _process_in_background(*args):
    try:
        _process(*args)
    except Exception:  # noqa: BLE001 - _process đã tự xử lý lỗi; đây là lưới an toàn cuối
        logger.exception("[Ingest] Lỗi luồng xử lý nền")


def ingest_bytes(
    raw: bytes,
    filename: str,
    *,
    title: str | None = None,
    category: str | None = None,
    uploaded_by: int | None = None,
    embeddings: EmbeddingService | None = None,
    background: bool = False,
) -> IngestResult:
    """Nạp 1 tài liệu từ nội dung bytes (dùng cho API upload và CLI).

    background=True: kiểm tra & lưu metadata xong thì trả ngay status
    'processing' (API trả 202), việc embed chạy ở luồng nền; client hỏi lại
    trạng thái qua GET /api/knowledge/<id>. Dùng cho file lớn để request HTTP
    không bị timeout.
    """
    embeddings = embeddings or get_embedding_service()

    try:
        source_type = detect_source_type(filename)
    except DocumentLoadError as exc:
        return IngestResult("failed", str(exc), reason="validation")

    max_bytes = KNOWLEDGE_MAX_FILE_MB * 1024 * 1024
    if not raw:
        return IngestResult("failed", "File rỗng.", reason="validation")
    if len(raw) > max_bytes:
        return IngestResult("failed", f"File vượt quá giới hạn {KNOWLEDGE_MAX_FILE_MB} MB.",
                            reason="validation")

    # Trích xuất text TRƯỚC khi ghi DB: file hỏng bị từ chối ngay, không để
    # lại bản ghi rác trong knowledge_metadata.
    try:
        sections = load_document_bytes(raw, filename)
    except DocumentLoadError as exc:
        return IngestResult("failed", str(exc), reason="validation")

    content_hash = sha256_bytes(raw)
    title = (title or os.path.splitext(os.path.basename(filename))[0]).strip()[:255]

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            existing = repository.find_document_by_hash(cursor, content_hash)
            if existing and existing["status"] == "ready":
                return IngestResult(
                    "duplicate",
                    f"Tài liệu đã tồn tại (#{existing['id']} - {existing['title']}).",
                    existing["id"], existing["chunk_count"],
                )
            if existing and existing["status"] == "processing" and not _is_stale(existing):
                return IngestResult("processing", "Tài liệu này đang được xử lý.",
                                    existing["id"])

            storage_path = _store_file(raw, filename, content_hash)
            if existing:
                # Lần trước thất bại (vd. hết quota) -> xử lý lại trên bản ghi cũ.
                document_id = existing["id"]
                repository.mark_processing(cursor, document_id)
            else:
                document_id = repository.create_document(
                    cursor,
                    title=title,
                    source_type=source_type,
                    content_hash=content_hash,
                    original_filename=os.path.basename(filename)[:255],
                    storage_path=storage_path,
                    mime_type=MIME_TYPES.get(source_type),
                    file_size_bytes=len(raw),
                    category=category,
                    uploaded_by=uploaded_by,
                )
        connection.commit()
    except pymysql.err.IntegrityError:
        # 2 request upload cùng 1 file đồng thời -> request sau thua UNIQUE key.
        connection.rollback()
        return IngestResult("processing", "Tài liệu này đang được xử lý bởi yêu cầu khác.")
    except Exception as exc:  # noqa: BLE001
        connection.rollback()
        logger.exception("[Ingest] Lỗi DB khi tạo metadata cho '%s'", filename)
        return IngestResult("failed", f"Lỗi cơ sở dữ liệu: {type(exc).__name__}", reason="system")
    finally:
        connection.close()

    if background:
        _executor.submit(_process_in_background, document_id, title, sections, None,
                         embeddings, "created")
        return IngestResult("processing", "Đã nhận tài liệu, đang lập chỉ mục ở chế độ nền.",
                            document_id)
    return _process(document_id, title, sections, None, embeddings, "created")


def ingest_file(path: str, **kwargs) -> IngestResult:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return IngestResult("failed", f"Không đọc được file '{path}': {exc}", reason="validation")
    return ingest_bytes(raw, os.path.basename(path), **kwargs)


def _build_faq_chunks(faqs: list[dict]) -> list[Chunk]:
    """Mỗi FAQ -> 1 chunk tự đủ nghĩa (chủ đề + câu hỏi mẫu + trả lời chuẩn)."""
    chunks: list[Chunk] = []
    for faq in faqs:
        intent = normalize_text(faq.get("intent") or "")
        header = f"Chủ đề: {intent}" if intent else ""
        parts = [
            f"Nhóm: {normalize_text(faq['nhom_nghiep_vu'])}" if faq.get("nhom_nghiep_vu") else "",
            header,
            f"Câu hỏi thường gặp:\n{normalize_text(faq['cau_hoi_mau'])}" if faq.get("cau_hoi_mau") else "",
            f"Hướng xử lý: {normalize_text(faq['xu_ly_tinh_huong'])}" if faq.get("xu_ly_tinh_huong") else "",
            f"Trả lời chuẩn: {normalize_text(faq['tra_loi_chuan'])}",
        ]
        text = "\n".join(part for part in parts if part)
        metadata = {"faq_id": faq["id"], "intent": intent}

        if len(text) <= FAQ_SINGLE_CHUNK_MAX_CHARS:
            pieces = [text]
        else:
            # Lặp lại dòng chủ đề ở mọi mảnh để mảnh nào cũng biết mình thuộc FAQ nào.
            pieces = [
                f"{header}\n{piece}" if header and not piece.startswith(header) else piece
                for piece in split_text(text, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
            ]
        for piece in pieces:
            chunks.append(Chunk(index=len(chunks), content=piece, metadata=dict(metadata)))
    return chunks


def sync_faqs(force: bool = False, embeddings: EmbeddingService | None = None) -> IngestResult:
    """Đồng bộ bảng faqs vào KB. Chỉ embed lại khi nội dung FAQ thay đổi
    (so sánh hash) hoặc force=True -> chạy lại thường xuyên không tốn quota."""
    embeddings = embeddings or get_embedding_service()

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            faqs = repository.fetch_active_faqs(cursor)
            if not faqs:
                return IngestResult("failed", "Bảng faqs chưa có dữ liệu (chạy import_faqs.py trước).",
                                    reason="validation")

            chunks = _build_faq_chunks(faqs)
            digest_source = "\n\x1e".join(chunk.content for chunk in chunks)
            content_hash = sha256_bytes(f"faq:{digest_source}".encode("utf-8"))

            document = repository.get_faq_document(cursor)
            if (document and not force and document["status"] == "ready"
                    and document["content_hash"] == content_hash
                    and document["embedding_model"] == embeddings.model):
                return IngestResult("unchanged", "FAQ không thay đổi, bỏ qua.",
                                    document["id"], document["chunk_count"])

            if document:
                document_id = document["id"]
                repository.mark_processing(cursor, document_id, content_hash)
                success_status = "updated"
            else:
                document_id = repository.create_document(
                    cursor,
                    title=FAQ_DOCUMENT_TITLE,
                    source_type="faq",
                    content_hash=content_hash,
                    category="faq",
                )
                success_status = "created"
        connection.commit()
    except Exception as exc:  # noqa: BLE001
        connection.rollback()
        logger.exception("[Ingest] Lỗi DB khi đồng bộ FAQ")
        return IngestResult("failed", f"Lỗi cơ sở dữ liệu: {type(exc).__name__}", reason="system")
    finally:
        connection.close()

    logger.info("[Ingest] Đồng bộ %s FAQ -> %s chunk.", len(faqs), len(chunks))
    return _process(document_id, FAQ_DOCUMENT_TITLE, None, chunks, embeddings, success_status)


def reindex_document(document_id: int, embeddings: EmbeddingService | None = None) -> IngestResult:
    embeddings = embeddings or get_embedding_service()

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            document = repository.get_document(cursor, document_id)
    finally:
        connection.close()

    if not document:
        return IngestResult("failed", f"Không tìm thấy tài liệu #{document_id}.",
                            reason="not_found")
    if document["source_type"] == "faq":
        return sync_faqs(force=True, embeddings=embeddings)

    path = document["storage_path"]
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        sections = load_document_bytes(raw, document["original_filename"] or path)
    except (OSError, DocumentLoadError) as exc:
        _mark_failed(document_id, str(exc))
        return IngestResult("failed", f"Không đọc lại được file gốc: {exc}", document_id,
                            reason="validation")

    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            repository.mark_processing(cursor, document_id)
        connection.commit()
    finally:
        connection.close()

    return _process(document_id, document["title"], sections, None, embeddings, "updated")


def set_document_active(document_id: int, is_active: bool) -> bool:
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            changed = repository.set_document_active(cursor, document_id, is_active)
        connection.commit()
    finally:
        connection.close()
    _notify_index_changed()
    return changed > 0


def delete_document(document_id: int, remove_file: bool = True) -> bool:
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            document = repository.get_document(cursor, document_id)
            if not document:
                return False
            repository.delete_document(cursor, document_id)
        connection.commit()
    finally:
        connection.close()

    path = document.get("storage_path")
    if remove_file and path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as exc:
            logger.warning("[Ingest] Không xoá được file %s: %s", path, exc)
    _notify_index_changed()
    return True
