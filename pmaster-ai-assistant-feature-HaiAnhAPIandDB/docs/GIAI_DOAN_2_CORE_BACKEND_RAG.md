# Giai đoạn 2 – Core Backend RAG (Flask + Gemini)

## Luồng xử lý 1 câu hỏi (`POST /api/chat`)

```
routes/chat.py
 1. user / conversation (phiên)            -> database.py
 2. đang chờ/đang gặp tư vấn viên?  -> chỉ lưu, không gọi AI (D1-09)
 3. Prompt Injection / xin dữ liệu nội bộ / kiểm duyệt  -> từ chối (D1-07, D1-13)
 4. FAQ khớp trực tiếp (faq_matcher)        -> trả "trả lời chuẩn", answer_status='faq'
 5. rag_service.answer():
      a. truy vấn KB (knowledge/retriever: vector -> FULLTEXT -> FAQ keyword)
      b. ghép [Nguồn n] vào RETRIEVED DATA của system prompt (ngân sách ký tự)
      c. gemini_client: gọi Gemini (retry lỗi tạm thời, marker trạng thái)
 6. lưu reply + answer_status + retrieved_chunk_ids + latency_ms
 7. cannot_answer 3 lần liên tiếp -> chuyển tư vấn viên
```

## File chính

| File | Vai trò |
|---|---|
| `main.py` | Điểm chạy: kiểm tra .env, ping MySQL, nạp sẵn chỉ mục KB, chạy server |
| `config.py` | Toàn bộ cấu hình qua biến môi trường |
| `rag_service.py` | Retrieval + ghép context + gọi Gemini + trả nguồn trích dẫn |
| `gemini_client.py` | google-genai: system prompt, retry, phân loại lỗi |
| `routes/health.py` | `GET /api/health` |

## Chạy

```bash
python main.py                    # PyCharm: Run 'main'
curl localhost:5000/api/health
```
