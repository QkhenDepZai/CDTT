# Giai đoạn 1 – Cơ sở dữ liệu MySQL & Knowledge Base (RAG)

## 1. Cấu trúc thêm mới

```
sql/
  01_init_schema.sql          # Cài mới toàn bộ DB (idempotent)
  02_migration_v1_to_v2.sql   # Nâng cấp DB cũ (DB_PythonMaster.sql) lên v2
  03_create_app_user.sql      # (Khuyến nghị) user MySQL quyền tối thiểu cho app
knowledge/
  document_loader.py          # PDF / TXT / MD / CSV / DOCX -> text (chuẩn hoá NFC)
  text_chunker.py             # Recursive chunking có overlap
  embedding_service.py        # Gemini Embedding: batch, retry, L2-normalize, cache
  repository.py               # SQL cho knowledge_metadata / knowledge_chunks
  vector_index.py             # Chỉ mục cosine trong RAM (numpy)
  retriever.py                # search(câu hỏi) -> top-K chunk (+ fallback FULLTEXT)
  ingest_service.py           # Điều phối nạp / đồng bộ FAQ / reindex / bật-tắt / xoá
manage_knowledge.py           # CLI quản trị KB
tests/                        # pytest: unit + integration (MySQL thật)
```

## 2. Ánh xạ bảng theo yêu cầu

| Yêu cầu            | Bảng / View                                    |
|--------------------|------------------------------------------------|
| users              | `users` (+ `user_channel_identities` cho PSID Messenger / Zalo user_id) |
| User sessions      | `conversations` (status bot/agent, fail_count, channel, summary) |
| chat_history       | bảng `messages` + VIEW `chat_history` (phẳng theo `user_id`) |
| knowledge_metadata | `knowledge_metadata` (1 dòng / tài liệu) + `knowledge_chunks` (đoạn + vector) |

`messages` giữ nguyên tên để toàn bộ route hiện có (`routes/chat.py`, `routes/staff.py`,
`conversation_memory.py`) chạy tiếp không phải sửa.

## 3. Chạy thử

```bash
pip install -r requirements.txt
cp .env.example .env                     # điền DB_PASSWORD, GEMINI_API_KEY
mysql -u root -p < sql/01_init_schema.sql
python import_faqs.py                    # Excel -> bảng faqs
python manage_knowledge.py -v sync-faq   # bảng faqs -> knowledge_chunks (embedding)
python manage_knowledge.py ingest data/knowledge/ --category the_le
python manage_knowledge.py list
python manage_knowledge.py search "Bảng A thi bao nhiêu phút?"
```

Test:

```bash
pytest tests -q                                            # unit test, không cần DB
PM_TEST_MYSQL=1 DB_PASSWORD=... pytest tests -q            # + integration (DB *_test riêng)
```

## 4. Hiệu chỉnh ngưỡng `RAG_MIN_SIMILARITY`

`search` mặc định `--min-score 0` để thấy toàn bộ điểm. Chạy bộ câu hỏi thử của BTC:
điểm của câu trả lời đúng thường nằm cao hơn hẳn nhóm câu hỏi ngoài phạm vi. Đặt
ngưỡng vào khoảng giữa 2 nhóm (mặc định 0.55) trong `.env`.
