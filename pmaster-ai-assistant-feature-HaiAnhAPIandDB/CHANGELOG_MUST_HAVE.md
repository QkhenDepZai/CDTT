# Cập nhật code theo các Must Have của URD Module D1

## Đã cập nhật
- D1-07: kiểm soát nội dung vi phạm + ghi `violation_logs`.
- D1-09: handover 3 lần AI không trả lời, tạo `agent_notifications`, staff claim/reply/close.
- D1-10: thêm retrieval context từ top FAQ trước khi gọi Gemini; Gemini ưu tiên nguồn nội bộ và không tự bịa.
- D1-13: phát hiện Prompt Injection ở backend, ghi log và từ chối trước khi gọi Gemini.
- Site isolation: `site_key` có thể truyền bằng body hoặc `X-Site-Key`, kiểm tra domain theo host chính xác.
- Staff access: các API staff kiểm tra `users.role = 'staff'` thay vì tin trực tiếp `staff_id`.
- D1-03 hỗ trợ endpoint lấy danh sách lịch sử phiên chat theo user.
- Retry Gemini 503 vẫn giữ nguyên và được gom thành hàm dùng chung.
- DB schema được gom thành một schema hoàn chỉnh, bao gồm image, handover, violation log và notification.

## Lưu ý
Bản này không tự tạo hệ thống đăng nhập staff, email notification hay dashboard frontend hoàn chỉnh. Backend đã có API nền cho notification/handover; UI có thể nối vào sau.

## Bảo mật
File `.env` chứa API key/mật khẩu đã được loại khỏi gói bàn giao. Hãy tạo `.env` từ `.env.example` và đặt key thật ở máy chạy ứng dụng.
