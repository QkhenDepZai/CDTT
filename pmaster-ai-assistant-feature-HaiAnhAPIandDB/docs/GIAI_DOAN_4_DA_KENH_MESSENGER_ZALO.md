# Giai đoạn 4 – Tích hợp đa kênh (Website, Facebook Messenger, Zalo OA)

## 1. Kiến trúc

```
Website widget ──REST /api/chat*──────────────┐
Messenger ──POST /webhooks/messenger──┐        │
Zalo OA  ──POST /webhooks/zalo────────┤        │
                                      ▼        ▼
            routes/webhooks.py   routes/chat.py
            (xác thực chữ ký,    (site_key, user_token)
             trả 200 ngay)              │
                   │                    │
     channels/dispatcher.py             │
     (chống trùng, hàng đợi nền,        │
      khoá tuần tự theo người dùng)     │
                   └──────► chat_service.process_message ◄──┘
                            (bảo mật → FAQ → RAG → handover)
                                      │
     channels/messenger.py / zalo.py ◄┘  gửi trả lời về đúng nền tảng
```

- **Một lõi nghiệp vụ duy nhất** (`chat_service.py`): mọi kênh có cùng hành vi chặn injection, FAQ, RAG,
  chuyển tư vấn viên. Thêm kênh mới = viết 1 lớp con `ChannelAdapter` (3 hàm) + đăng ký ở `channels/registry.py`.
- **Tư vấn viên trả lời 2 chiều**: `POST /api/staff/conversations/{id}/reply` tự đẩy tin ra Messenger/Zalo
  nếu phiên thuộc các kênh đó (`channel_delivered` trong response).
- **Chống trùng**: Facebook/Zalo gửi lại webhook khi phản hồi chậm; bảng `channel_inbound_events`
  (UNIQUE `channel, event_key`) đảm bảo mỗi tin chỉ được trả lời 1 lần, kể cả chạy nhiều worker.

| Tính năng | Messenger | Zalo OA |
|---|---|---|
| Xác thực webhook | `X-Hub-Signature-256` = HMAC-SHA256(App Secret, body) | `X-ZEvent-Signature` = `mac=` SHA256(app_id + body + timestamp + OA Secret) |
| Lời chào + FAQ gợi ý (D1-01) | Nút "Bắt đầu" → quick reply | Sự kiện `follow` → danh sách gợi ý dạng text |
| Ảnh (D1-02) | Tải từ CDN, kiểm tra ≤ 5MB, JPG/PNG/WEBP | như Messenger |
| Gặp tư vấn viên (D1-09) | Quick reply hoặc gõ "gặp tư vấn viên" | Gõ "gặp tư vấn viên" |
| Markdown | Chuyển sang text thường, giữ thụt lề code | như Messenger |
| Tin dài | Tự cắt ≤ 2000 ký tự/tin | như Messenger |
| Token | Page Access Token dài hạn | Tự làm mới bằng refresh token, lưu `channel_tokens` |

## 2. Chuẩn bị chung

1. Chạy lại migration (tạo `channel_inbound_events`, `channel_tokens`):
   `mysql -u root -p < sql/02_migration_v1_to_v2.sql` (an toàn khi chạy lại).
2. Webhook phải là **HTTPS công khai**. Khi phát triển trên máy: `ngrok http 5000`
   (hoặc `cloudflared tunnel --url http://localhost:5000`) → dùng URL `https://xxxx.ngrok-free.app`.
3. `python main.py` → log sẽ in `Kênh messenger: BẬT` / `Kênh zalo: BẬT` khi đủ khoá.

## 3. Facebook Messenger

1. developers.facebook.com → Tạo App (loại *Business*) → thêm sản phẩm **Messenger**.
2. *Messenger API Settings*: gắn Fanpage → **Generate token** → `FB_PAGE_ACCESS_TOKEN`.
3. *App settings → Basic*: **App Secret** → `FB_APP_SECRET`.
4. Tự đặt 1 chuỗi ngẫu nhiên → `FB_VERIFY_TOKEN`.
5. *Webhooks → Add Callback URL*: `https://<domain>/webhooks/messenger`, Verify token = `FB_VERIFY_TOKEN`
   → **Verify and save**; bật trường `messages`, `messaging_postbacks`.
6. (Khuyến nghị) Cài nút "Bắt đầu":
   ```bash
   curl -X POST "https://graph.facebook.com/v23.0/me/messenger_profile?access_token=<PAGE_TOKEN>" \
        -H "Content-Type: application/json" -d '{"get_started":{"payload":"GET_STARTED"}}'
   ```
7. Khi app ở chế độ *Development*, chỉ tài khoản có vai trò trong app nhắn được. Muốn công khai cần
   gửi App Review quyền `pages_messaging`.

Chính sách 24 giờ: bot chỉ được nhắn trong 24h kể từ tin cuối của người dùng. Tư vấn viên cần trả lời
muộn hơn → xin quyền *Human Agent* rồi đặt `FB_USE_HUMAN_AGENT_TAG=true` (cho phép tới 7 ngày).

## 4. Zalo Official Account

1. developers.zalo.me → Tạo ứng dụng → liên kết OA → lấy **App ID** (`ZALO_APP_ID`) và
   **Khóa bí mật của ứng dụng** (`ZALO_APP_SECRET`).
2. Mục *Official Account → Webhook*: URL `https://<domain>/webhooks/zalo`; lấy **OA Secret Key**
   (`ZALO_OA_SECRET_KEY`); bật sự kiện `user_send_text`, `user_send_image`, `follow`.
3. Xác thực domain webhook theo hướng dẫn của Zalo (file HTML/meta tag trên domain).
4. Lấy token lần đầu qua OAuth v4 của OA (cấp quyền cho ứng dụng) → điền `ZALO_ACCESS_TOKEN`,
   `ZALO_REFRESH_TOKEN`, `ZALO_OA_ID`. Từ đó hệ thống **tự làm mới** và lưu token mới vào bảng
   `channel_tokens` (refresh token của Zalo đổi sau mỗi lần làm mới – không cần sửa `.env` nữa).
5. Tin "tư vấn" (`/v3.0/oa/message/cs`) chỉ gửi được cho người đã tương tác với OA trong khung thời
   gian Zalo cho phép.

## 5. Kiểm thử

- **Postman**: thư mục *5. Webhook đa kênh (mô phỏng)*. Điền `fbAppSecret`, `fbVerifyToken`,
  `zaloAppId`, `zaloOaSecretKey` trong environment (trùng `.env`); Pre-request Script tự ký request
  giống hệt Facebook/Zalo. Với PSID/user_id giả, bước gửi trả lời sẽ bị nền tảng từ chối (xem log) –
  đó là hành vi đúng; dùng ID thật của tài khoản test để nhận tin.
- **pytest**: `tests/test_channels_unit.py` (chữ ký, parse, định dạng) và
  `tests/test_channels_integration.py` (toàn luồng trên MySQL, Graph/Zalo API giả lập).
- `GET /api/health` → `"channels": {"web": true, "messenger": true, "zalo": false}`.

## 6. Mở rộng khi tải lớn

Hàng đợi hiện tại là `ThreadPoolExecutor` trong tiến trình (đủ cho vài chục tin/giây). Khi cần độ bền
cao hơn (không mất tin khi restart giữa chừng), thay `dispatcher._executor.submit` bằng hàng đợi ngoài
(Redis RQ / Celery / RabbitMQ) — chữ ký hàm `process_event(adapter, event)` giữ nguyên.
