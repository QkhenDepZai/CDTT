"""
Công cụ dòng lệnh quản trị Knowledge Base (RAG).

Chạy trong thư mục dự án (đã có .env). Trong PyCharm: Run > Edit
Configurations > Script path = manage_knowledge.py, Parameters = lệnh bên dưới.

    python manage_knowledge.py ingest data/the_le_2026.pdf --category the_le
    python manage_knowledge.py ingest data/knowledge/           # cả thư mục
    python manage_knowledge.py sync-faq [--force]               # bảng faqs -> KB
    python manage_knowledge.py list
    python manage_knowledge.py search "Lệ phí thi bảng A là bao nhiêu?" --top-k 5
    python manage_knowledge.py deactivate 3 | activate 3 | delete 3
    python manage_knowledge.py reindex 3 | reindex --all
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from database import get_db_connection
from knowledge import ingest_service, repository
from knowledge.document_loader import SUPPORTED_EXTENSIONS

logger = logging.getLogger("pmaster.manage_knowledge")


def _iter_files(paths: list[str]):
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for name in sorted(files):
                    ext = os.path.splitext(name)[1].lower().lstrip(".")
                    if ext in SUPPORTED_EXTENSIONS:
                        yield os.path.join(root, name)
        else:
            yield path


def _print_result(label: str, result: ingest_service.IngestResult):
    icon = "OK " if result.ok else "LỖI"
    doc = f"#{result.document_id}" if result.document_id else "-"
    print(f"[{icon}] {label}: {result.status} {doc} ({result.chunk_count} chunk) - {result.message}")


def cmd_ingest(args) -> int:
    failures = 0
    files = list(_iter_files(args.paths))
    if not files:
        print("Không tìm thấy file hợp lệ (pdf, txt, md, csv, docx).")
        return 1
    for path in files:
        title = args.title if args.title and len(files) == 1 else None
        result = ingest_service.ingest_file(path, title=title, category=args.category)
        _print_result(path, result)
        failures += 0 if result.ok else 1
    return 1 if failures else 0


def cmd_sync_faq(args) -> int:
    result = ingest_service.sync_faqs(force=args.force)
    _print_result("faqs", result)
    return 0 if result.ok else 1


def cmd_list(args) -> int:
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            documents = repository.list_documents(cursor, limit=args.limit)
    finally:
        connection.close()

    if not documents:
        print("Knowledge Base đang trống.")
        return 0
    print(f"{'ID':>4}  {'Loại':<5} {'Trạng thái':<11} {'Bật':<3} {'Chunk':>5}  Tiêu đề")
    for doc in documents:
        print(
            f"{doc['id']:>4}  {doc['source_type']:<5} {doc['status']:<11} "
            f"{'x' if doc['is_active'] else '-':<3} {doc['chunk_count']:>5}  {doc['title']}"
            + (f"  [lỗi: {doc['error_message']}]" if doc["status"] == "failed" else "")
        )
    return 0


def cmd_search(args) -> int:
    from knowledge.retriever import get_retriever

    results = get_retriever().search(args.query, top_k=args.top_k, min_score=args.min_score)
    if not results:
        print("Không tìm thấy đoạn tri thức nào đủ liên quan "
              "(AI sẽ trả lời 'không rõ' và hướng dẫn gặp tư vấn viên).")
        return 0
    for rank, item in enumerate(results, start=1):
        preview = item.content.replace("\n", " ")
        preview = preview[:220] + ("…" if len(preview) > 220 else "")
        print(f"{rank}. score={item.score:.4f} [{item.retrieval_method}] {item.citation()}")
        print(f"   {preview}")
    return 0


def cmd_set_active(args, is_active: bool) -> int:
    ok = ingest_service.set_document_active(args.document_id, is_active)
    print("Đã cập nhật." if ok else f"Không tìm thấy tài liệu #{args.document_id}.")
    return 0 if ok else 1


def cmd_delete(args) -> int:
    ok = ingest_service.delete_document(args.document_id, remove_file=not args.keep_file)
    print("Đã xoá." if ok else f"Không tìm thấy tài liệu #{args.document_id}.")
    return 0 if ok else 1


def cmd_reindex(args) -> int:
    if args.all:
        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                ids = [doc["id"] for doc in repository.list_documents(cursor, limit=100000)]
        finally:
            connection.close()
    elif args.document_id:
        ids = [args.document_id]
    else:
        print("Cần truyền document_id hoặc --all.")
        return 2

    failures = 0
    for document_id in ids:
        result = ingest_service.reindex_document(document_id)
        _print_result(f"#{document_id}", result)
        failures += 0 if result.ok else 1
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quản trị Knowledge Base cho chatbot RAG.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Hiện log chi tiết")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="Nạp file hoặc thư mục tài liệu")
    p.add_argument("paths", nargs="+")
    p.add_argument("--title", help="Tiêu đề (chỉ áp dụng khi nạp 1 file)")
    p.add_argument("--category", help="Nhóm tài liệu: the_le, quy_che, de_thi, huong_dan...")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("sync-faq", help="Đồng bộ bảng faqs vào Knowledge Base")
    p.add_argument("--force", action="store_true", help="Embed lại kể cả khi không đổi")
    p.set_defaults(func=cmd_sync_faq)

    p = sub.add_parser("list", help="Liệt kê tài liệu")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("search", help="Thử truy vấn RAG")
    p.add_argument("query")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--min-score", type=float, default=0.0,
                   help="Mặc định 0 để xem cả điểm thấp khi hiệu chỉnh ngưỡng")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("deactivate", help="Tắt tài liệu khỏi RAG")
    p.add_argument("document_id", type=int)
    p.set_defaults(func=lambda a: cmd_set_active(a, False))

    p = sub.add_parser("activate", help="Bật lại tài liệu")
    p.add_argument("document_id", type=int)
    p.set_defaults(func=lambda a: cmd_set_active(a, True))

    p = sub.add_parser("delete", help="Xoá hẳn tài liệu")
    p.add_argument("document_id", type=int)
    p.add_argument("--keep-file", action="store_true", help="Giữ file gốc trên đĩa")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("reindex", help="Embed lại tài liệu")
    p.add_argument("document_id", type=int, nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_reindex)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI: in lỗi gọn, chi tiết ở log
        logger.exception("Lệnh '%s' thất bại", args.command)
        print(f"Lỗi: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
