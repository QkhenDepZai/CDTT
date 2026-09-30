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

## Widget & hoàn thiện Module D1 (09/2026)
Hướng dẫn đầy đủ: `docs/HUONG_DAN_SU_DUNG_WIDGET.md`.
- Widget nhúng 1 dòng `widget.js` (Shadow DOM, responsive): lời chào + FAQ gợi ý, markdown/khối code, gửi ảnh có
  preview, hỏi lại kèm nút chọn nhanh, gặp tư vấn viên, form ticket ngoài giờ, lịch sử, tải .txt/PDF.
- Staff Dashboard `/staff` (hàng chờ, tiếp nhận, trả lời, đóng phiên, ticket, thông báo) và trang quản trị
  `/admin` (FAQ CRUD, tài liệu Knowledge Base, báo cáo + xuất Excel).
- `guardrails.py`: câu từ chối chuẩn BA, danh mục từ khoá cấm `data/banned_keywords.txt`, chặn rò rỉ KB/cấu
  hình/dữ liệu người khác, chặn câu ngoài phạm vi rõ ràng.
- `track_advisor.py`: xác định Bảng A/B theo tuổi/năm sinh/cấp học, hỏi lại khi thiếu (sửa độ tuổi Bảng A 13–18).
- `handover.py`: giờ trực, ticket ngoài giờ, email thông báo tư vấn viên; chặn 2 nhân viên cùng nhận 1 phiên.
- Sửa ô dữ liệu lỗi trong `danh_sach_faq.xlsx` cho khớp bảng BA.
