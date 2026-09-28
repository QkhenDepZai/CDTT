# Giai đoạn 3 – RESTful API & Postman

## 1. Import vào Postman

1. Postman → **Import** → chọn 2 file trong thư mục `postman/`:
   - `PythonMaster_Chatbot.postman_collection.json`
   - `PythonMaster_Local.postman_environment.json`
2. Chọn environment **PythonMaster - Local**, điền `adminKey` = giá trị `ADMIN_API_KEY` trong `.env`.
3. Chạy `python main.py`, rồi **Run collection** (chạy tuần tự từ trên xuống). Các request tự lưu
   `userId`, `userToken`, `conversationId`, `documentId` cho request sau.
4. Request upload file dùng `postman/sample_*.{txt,png}`. Nếu Postman báo không tìm thấy file:
   mở tab *Body* của request và chọn lại file (hoặc Settings → *Working directory* = thư mục `postman/`).

Chạy bằng dòng lệnh (CI): `npx newman run postman/PythonMaster_Chatbot.postman_collection.json -e postman/PythonMaster_Local.postman_environment.json --env-var adminKey=... --working-dir postman`

## 2. Cấu hình bảo mật (`.env`)

| Biến | Ý nghĩa |
|---|---|
| `APP_SECRET_KEY` | Khoá ký `user_token` (≥ 32 ký tự). Thiếu → `/api/history/*` từ chối mọi yêu cầu |
| `ADMIN_API_KEY` | Khoá header `X-Admin-Key` cho `/api/knowledge/*`. Thiếu → API quản trị bị khoá (503) |
| `ENFORCE_USER_TOKEN` | `true` → `/api/chat*` bắt buộc `X-User-Token` khi gửi `user_id` có sẵn |

Tạo khoá: `python -c "import secrets; print(secrets.token_urlsafe(48))"`

## 3. Endpoint chính

### POST /api/chat — hỏi đáp RAG
Header: `X-Site-Key: site_demo_001`, `X-User-Token: <user_token>` (tuỳ chọn, bắt buộc nếu `ENFORCE_USER_TOKEN=true`)

```json
{ "message": "Bảng A dành cho độ tuổi nào?", "user_id": 12, "conversation_id": 34 }
```
Bỏ `user_id`/`conversation_id` để hệ thống tự tạo mới. Phản hồi:
```json
{
  "status": "success",
  "user_id": 12,
  "user_token": "wJK5s8mD...",
  "conversation_id": 34,
  "reply": "Bảng A dành cho học sinh từ 12 đến 18 tuổi.",
  "used_faq": false,
  "answer_status": "answered",
  "sources": [{ "title": "Thể lệ 2026", "citation": "Thể lệ 2026 (tr.3)", "score": 0.82, "method": "vector" }],
  "latency_ms": 1240
}
```
`answer_status`: `answered` | `cannot_answer` | `out_of_scope` | `rate_limited` | `server_error` | ...
Khi `cannot_answer` 3 lần liên tiếp: thêm `"escalated": true, "fail_count": 3`.

### GET /api/history/{user_id} — lịch sử chat
Header bắt buộc: `X-User-Token`. Query: `limit` (≤ 100), `conversation_id`, `before_id`.
```json
{
  "status": "success", "user_id": 12, "count": 2, "next_before_id": 36,
  "messages": [
    { "message_id": 37, "conversation_id": 34, "channel": "web", "sender_type": "model",
      "content": "...", "image_url": null, "answer_status": "answered", "created_at": "2026-09-28 14:23:45" }
  ]
}
```
Trang sau: gửi `before_id=<next_before_id>`; `next_before_id = null` là hết dữ liệu.
Thêm: `GET /api/history/{user_id}/conversations`, `GET /api/history/{user_id}/export?conversation_id=34` (file .txt).

### POST /api/knowledge/upload — thêm tài liệu (Admin)
Header `X-Admin-Key`. Body **form-data**: `file` (PDF/TXT/CSV/DOCX/MD ≤ 20MB), `title`, `category`, `background` (`true`/`false`).
```json
{ "status": "success", "result": "created", "document_id": 5, "chunk_count": 14,
  "message": "Đã lập chỉ mục 14 đoạn tri thức.", "reason": null }
```

| HTTP | `result` / `reason` | Ý nghĩa |
|---|---|---|
| 201 | created | Đã lập chỉ mục, có hiệu lực ngay |
| 200 | duplicate | File (cùng SHA-256) đã có |
| 202 | processing | `background=true`: theo dõi bằng `GET /api/knowledge/{id}` |
| 400 | – | Thiếu file |
| 413 | – | Quá dung lượng |
| 422 | failed / validation | Sai định dạng, file hỏng, PDF scan không có text |
| 503 | failed / embedding | Gemini Embedding lỗi/hết quota – upload lại sau |

Các API quản trị khác: `POST /api/knowledge/text`, `GET /api/knowledge`, `GET|PATCH|DELETE /api/knowledge/{id}`,
`POST /api/knowledge/{id}/reindex`, `POST /api/knowledge/sync-faq?force=true`,
`POST /api/knowledge/search` (xem điểm cosine để hiệu chỉnh `RAG_MIN_SIMILARITY`).
