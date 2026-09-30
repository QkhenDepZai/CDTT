# Hướng dẫn sử dụng — Widget Chatbot AI Python Master 2026 (Module D1)

Tài liệu dành cho: **Dev** (cài đặt, nhúng widget), **QC** (kiểm thử theo `QC_D1_TEST_CASE`),
**Tư vấn viên** (Staff Dashboard) và **Quản trị nghiệp vụ** (FAQ, tài liệu, báo cáo).

Nguồn yêu cầu: `URD_ModuleD1.docx` (D1-01 … D1-13), `QC_D1_TEST_PLAN.docx`, `QC_D1_TEST_CASE.xlsx`,
bảng BA "Xác định câu hỏi và tình huống" (chính là `danh_sach_faq.xlsx`).

---

## Mục lục

1. [Thành phần hệ thống](#1-thành-phần-hệ-thống)
2. [Cài đặt và chạy thử trong 10 phút](#2-cài-đặt-và-chạy-thử-trong-10-phút)
3. [Nhúng widget vào website](#3-nhúng-widget-vào-website)
4. [Hướng dẫn cho thí sinh — dùng khung chat](#4-hướng-dẫn-cho-thí-sinh--dùng-khung-chat)
5. [Hướng dẫn cho tư vấn viên — Staff Dashboard](#5-hướng-dẫn-cho-tư-vấn-viên--staff-dashboard)
6. [Hướng dẫn cho quản trị — trang /admin](#6-hướng-dẫn-cho-quản-trị--trang-admin)
7. [Cấu hình (.env)](#7-cấu-hình-env)
8. [Cách chatbot xử lý 1 tin nhắn](#8-cách-chatbot-xử-lý-1-tin-nhắn)
9. [Bảng truy vết yêu cầu URD → chức năng → test case](#9-bảng-truy-vết-yêu-cầu-urd--chức-năng--test-case)
10. [Hướng dẫn kiểm thử cho QC](#10-hướng-dẫn-kiểm-thử-cho-qc)
11. [API mới](#11-api-mới)
12. [Chạy test tự động](#12-chạy-test-tự-động)
13. [Xử lý sự cố thường gặp](#13-xử-lý-sự-cố-thường-gặp)
14. [Giới hạn đã biết](#14-giới-hạn-đã-biết)

---

## 1. Thành phần hệ thống

| Thành phần | Đường dẫn (backend chạy ở `http://localhost:5000`) | File mã nguồn |
|---|---|---|
| Widget nhúng website | `/widget.js` | `frontend/widget.js` |
| Trang demo đã nhúng widget + nút câu hỏi mẫu | `/demo` | `frontend/demo.html` |
| Staff Dashboard (tư vấn viên) | `/staff` | `frontend/staff.html` |
| Trang quản trị (FAQ, tài liệu, báo cáo) | `/admin` | `frontend/admin.html` |
| Backend REST API | `/api/...` | `app.py`, `routes/`, `chat_service.py` … |

Module backend mới/viết lại trong đợt này:

| File | Vai trò |
|---|---|
| `guardrails.py` | Lớp an toàn chạy trước AI: Prompt Injection, rò rỉ dữ liệu, từ khoá cấm, ngoài phạm vi (D1-07/08/13) |
| `data/banned_keywords.txt` | Danh mục từ khoá cấm — quản trị tự sửa, không cần sửa code |
| `track_advisor.py` | Xác định Bảng A/B theo tuổi, năm sinh, cấp học; hỏi lại khi thiếu thông tin (D1-06) |
| `handover.py` | Chuyển tư vấn viên, giờ trực, ticket ngoài giờ (D1-09) |
| `notifier.py` | Email thông báo tư vấn viên (tuỳ chọn, qua SMTP) |
| `reports.py`, `routes/admin.py` | Quản trị FAQ, báo cáo thống kê, xuất Excel (D1-11/12) |
| `routes/pages.py` | Phục vụ widget và các trang giao diện |
| `chat_service.py` | Lõi xử lý hội thoại dùng chung cho Website / Messenger / Zalo |

---

## 2. Cài đặt và chạy thử trong 10 phút

Yêu cầu: **Python 3.10+**, **MySQL 8.0+** (cần FULLTEXT `ngram`, MariaDB không hỗ trợ), trình duyệt Chrome/Edge,
khoá Gemini API (https://aistudio.google.com/apikey).

```powershell
# 1) Cài thư viện (trong thư mục pmaster-ai-assistant-feature-HaiAnhAPIandDB)
python -m venv venv
venv\Scripts\activate            # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

# 2) Cấu hình
copy .env.example .env           # macOS/Linux: cp .env.example .env
#    Mở .env, điền tối thiểu: DB_PASSWORD, GEMINI_API_KEY, APP_SECRET_KEY, ADMIN_API_KEY
#    Tạo khoá ngẫu nhiên: python -c "import secrets; print(secrets.token_urlsafe(48))"

# 3) Khởi tạo database + nạp FAQ + đưa FAQ vào Knowledge Base (RAG)
python init_db.py
python import_faqs.py
python manage_knowledge.py -v sync-faq

# 4) (tuỳ chọn) Tạo thêm tài khoản tư vấn viên — DB mới đã có sẵn "nhan_vien_demo" (id = 1)
python create_staff.py tu_van_1

# 5) Chạy server
python main.py
```

Mở trình duyệt:

- **http://localhost:5000/demo** — trang demo, bấm nút chat ở góc phải dưới (hoặc thêm `?open=1` để tự mở).
- **http://localhost:5000/staff** — đăng nhập bằng mã tư vấn viên (vd `1`).
- **http://localhost:5000/admin** — đăng nhập bằng giá trị `ADMIN_API_KEY` trong `.env`.

> **Quan trọng:** phải đặt `APP_SECRET_KEY` (≥ 32 ký tự). Thiếu khoá này thì widget vẫn chat được nhưng
> **không** khôi phục phiên khi tải lại trang, **không** xem/tải lịch sử và **không** nhận được tin nhắn
> của tư vấn viên (các API đó bắt buộc `user_token` để chống xem trộm hội thoại của người khác — D1-13).

---

## 3. Nhúng widget vào website

Dán **1 dòng** trước thẻ `</body>` của website (thay `<backend>` bằng domain backend):

```html
<script src="https://<backend>/widget.js" data-site-key="site_demo_001" defer></script>
```

| Thuộc tính | Bắt buộc | Ý nghĩa |
|---|---|---|
| `data-site-key` | Có | Mã website trong bảng `sites`. Sai/thiếu → API trả 401 |
| `data-api-base` | Không | URL backend nếu khác nơi phục vụ `widget.js` |
| `data-title` | Không | Tiêu đề khung chat (mặc định "Trợ lý Python Master") |
| `data-position` | Không | `right` (mặc định) hoặc `left` |
| `data-open` | Không | `true` → tự mở khung chat khi tải trang |

**Cho phép domain gọi API** (bảng `sites`, cột `allowed_domain`; `*` = mọi domain — chỉ dùng khi test):

```sql
INSERT INTO sites (site_key, allowed_domain) VALUES ('site_pythonmaster', 'pythonmaster.vn');
UPDATE sites SET allowed_domain = 'localhost:8000' WHERE site_key = 'site_demo_001';
```

Request từ domain không nằm trong `allowed_domain` bị từ chối **403 "Domain không được phép"** (TC-API-03).

Widget chạy trong **Shadow DOM**: CSS của website không làm vỡ giao diện chat và ngược lại. Khi test có thể
điều khiển widget từ Console: `PythonMasterChat.open()`, `.close()`, `.newChat()`, `.send("Câu hỏi")`.

**Test theo Test Plan (web server riêng cổng 8000):**

```powershell
cd frontend
python -m http.server 8000
# mở http://localhost:8000/demo.html?api=http://localhost:5000
```

---

## 4. Hướng dẫn cho thí sinh — dùng khung chat

| | |
|---|---|
| ![Lời chào và câu hỏi gợi ý](images/widget_chao_faq.png) | ![Hỏi lại kèm lựa chọn nhanh](images/widget_hoi_lai.png) |

1. **Mở chat:** bấm biểu tượng SOTATEK ở góc phải dưới. Chatbot chào và gợi ý 5 câu hỏi thường gặp — bấm
   để hỏi ngay (câu trả lời lấy nguyên văn từ FAQ đã duyệt).
2. **Đặt câu hỏi:** gõ câu hỏi rồi nhấn **Enter** (Shift+Enter để xuống dòng). Câu trả lời từ Knowledge Base có
   dòng "Nguồn: …" bên dưới.
3. **Câu hỏi thiếu thông tin** (vd "Em thuộc bảng nào?"): chatbot hỏi lại kèm **nút chọn nhanh**. Bấm nút hoặc
   trả lời tự do ("Em 15 tuổi", "Em sinh năm 2010") — chatbot ghép với câu hỏi trước để trả lời.
4. **Hỏi Python:** đoạn code hiển thị trong khung code riêng, có nút **Sao chép**.
5. **Gửi ảnh** (ảnh lỗi code, ảnh màn hình lỗi đăng ký): bấm biểu tượng kẹp giấy hoặc **dán ảnh (Ctrl+V)**.
   Chỉ nhận **JPG, PNG, WEBP ≤ 5MB**. Ảnh hiện **xem trước** kèm nút ✕ để bỏ trước khi gửi, và vẫn hiển thị
   trong khung chat sau khi gửi.
6. **Gặp tư vấn viên:** bấm nút **Gặp tư vấn viên** dưới ô nhập. Trong giờ trực, phiên vào hàng chờ, thanh trạng
   thái chuyển "Đang chờ tư vấn viên…" rồi "Tư vấn viên đang hỗ trợ bạn"; tin nhắn của tư vấn viên tự hiện
   (viền xanh lá, nhãn "Tư vấn viên"). Nếu AI không trả lời được **3 lần liên tiếp**, hệ thống tự chuyển.
7. **Ngoài giờ trực:** chatbot báo chưa có nhân viên trực và hiện **form để lại thông tin** (họ tên, email/SĐT,
   nội dung, ô đồng ý lưu thông tin). Gửi xong nhận **mã yêu cầu**; trợ lý AI vẫn tiếp tục hỗ trợ.
8. **Lịch sử:** biểu tượng đồng hồ → danh sách các cuộc trò chuyện trước → bấm để mở lại.
9. **Tải xuống:** biểu tượng mũi tên xuống → **Tải file văn bản (.txt)** hoặc **Lưu thành PDF** (mở bản in,
   chọn đích "Lưu dưới dạng PDF").
10. **Cuộc trò chuyện mới:** biểu tượng **+** (phiên mới có mã riêng, không dùng lẫn ngữ cảnh phiên cũ).

Tải lại trang: phiên đang mở được **khôi phục** (trình duyệt chỉ lưu mã phiên, không lưu nội dung chat).
Trên điện thoại, khung chat hiển thị toàn màn hình.

| | | |
|---|---|---|
| ![Khối code](images/widget_code_block.png) | ![Tư vấn viên](images/widget_tu_van_vien.png) | ![Ngoài giờ](images/widget_ngoai_gio.png) |

---

## 5. Hướng dẫn cho tư vấn viên — Staff Dashboard

![Staff Dashboard](images/staff_dashboard.png)

1. Mở `/staff`, nhập **mã tư vấn viên** (tạo bằng `python create_staff.py <username>`).
2. Bấm **Bật thông báo trình duyệt** để nhận popup khi có yêu cầu mới (kèm âm báo và chuông 🔔 đếm số
   thông báo chưa đọc). Danh sách tự làm mới mỗi vài giây.
3. Tab **Đang chờ**: các phiên thí sinh yêu cầu gặp tư vấn viên hoặc AI thất bại 3 lần. Bấm vào phiên để xem
   **toàn bộ lịch sử** (tin thí sinh, trả lời AI, ảnh đính kèm).
4. Bấm **Tiếp nhận** → phiên chuyển sang tab **Của tôi**, ô trả lời xuất hiện. Hai nhân viên không thể cùng
   nhận 1 phiên (người sau nhận lỗi "không ở trạng thái chờ").
5. Nhập câu trả lời, **Enter** để gửi — thí sinh thấy ngay trên widget (hoặc Messenger/Zalo nếu phiên đến từ
   các kênh đó). Trong lúc này trợ lý AI **không** tự trả lời.
6. Xong việc bấm **Đóng phiên**. Thí sinh nhắn tiếp sẽ quay lại trò chuyện với trợ lý AI.
7. Tab **Ticket**: các yêu cầu để lại ngoài giờ (họ tên, email/SĐT, nội dung). Đổi trạng thái
   **Mới → Đang xử lý → Đã xong**, bấm **Xem phiên** để đọc hội thoại liên quan.

Email thông báo (tuỳ chọn): điền nhóm biến `SMTP_*` và `STAFF_NOTIFY_EMAILS` trong `.env`. Email chỉ ghi mã
phiên/ticket và lý do, không chứa nội dung hay thông tin liên hệ của thí sinh.

---

## 6. Hướng dẫn cho quản trị — trang /admin

Đăng nhập bằng `ADMIN_API_KEY` (khoá chỉ giữ trong tab trình duyệt đang mở).

**Tab FAQ (Hỏi – Đáp)** — D1-11, TC-RAG-05/06
- Thêm FAQ: nhập Nhóm, Intent, Câu hỏi mẫu (mỗi câu 1 dòng bắt đầu bằng `- `), Câu trả lời chuẩn → **Lưu FAQ**.
- **Sửa** / **Ẩn** FAQ trong bảng. FAQ ẩn không bị xoá (giữ lịch sử) nhưng chatbot không dùng nữa.
- Mọi thay đổi **có hiệu lực ngay**: bộ so khớp FAQ đọc thẳng bảng `faqs`, và hệ thống tự đồng bộ sang
  Knowledge Base. Nếu Gemini tạm lỗi khi đồng bộ, trang báo rõ — bấm lại "Đồng bộ FAQ" ở tab Tài liệu sau.

**Tab Tài liệu tri thức** — D1-11, TC-RAG-04/09
- Tải lên **PDF, Word (.docx), TXT, Markdown, CSV** (≤ 20MB). File rỗng/không đọc được bị từ chối kèm lý do.
- Bảng tài liệu: trạng thái xử lý, số đoạn; **Ẩn/Dùng lại**, **Xử lý lại**, **Xoá**.
- **Thử truy vấn Knowledge Base**: xem các đoạn tri thức và điểm tương đồng RAG dùng cho 1 câu hỏi.

**Tab Báo cáo** — D1-12, TC-RP-01…04

![Báo cáo](images/admin_bao_cao.png)

- Chọn **Ngày / Tuần / Tháng** và khoảng thời gian → ô tổng hợp, biểu đồ số câu hỏi (rê chuột xem chi tiết),
  bảng kết quả xử lý theo kỳ và top câu hỏi phổ biến.
- **Xuất Excel**: file gồm 2 sheet "Chưa trả lời được" và "Chuyển tư vấn viên" (thời gian, phiên, kênh,
  câu hỏi) — dùng để bổ sung FAQ.

**Danh mục từ khoá cấm:** sửa `data/banned_keywords.txt` (mỗi dòng 1 từ/cụm; viết có dấu = chỉ khớp văn bản
có dấu; viết không dấu = khớp cả khi gõ không dấu), sau đó khởi động lại server.

---

## 7. Cấu hình (.env)

Các biến cũ (MySQL, Gemini, RAG, Messenger/Zalo) giữ nguyên — xem `.env.example`. Biến **mới**:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `COMPETITION_YEAR` | `2026` | Năm thi, dùng tính tuổi từ năm sinh |
| `TRACK_A_MIN_AGE` / `TRACK_A_MAX_AGE` | `13` / `18` | Độ tuổi Bảng A (THCS, THPT) |
| `TRACK_B_MIN_AGE` / `TRACK_B_MAX_AGE` | `19` / `24` | Độ tuổi Bảng B (ĐH, CĐ) |
| `SUPPORT_CONTACT_TEXT` | email + hotline BTC | Chèn vào câu trả lời khi không có dữ liệu / ngoài giờ |
| `SUPPORT_HOURS` | *(trống = 24/7)* | Giờ trực tư vấn viên, vd `08:00-17:30` (hỗ trợ ca qua đêm `22:00-06:00`) |
| `SUPPORT_DAYS` | `1-7` | Ngày làm việc: 1 = Thứ 2 … 7 = Chủ nhật, vd `1-5` hoặc `1,2,3,4,5,6` |
| `SUPPORT_UTC_OFFSET_HOURS` | `7` | Múi giờ của giờ trực (Việt Nam = 7) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_FROM` | trống | Máy chủ gửi email thông báo |
| `STAFF_NOTIFY_EMAILS` | trống | Email nhận thông báo, cách nhau dấu phẩy |
| `STAFF_DASHBOARD_URL` | `http://localhost:5000/staff` | Link ghi trong email |

---

## 8. Cách chatbot xử lý 1 tin nhắn

```
Tin nhắn thí sinh (Website / Messenger / Zalo)
  │
  ├─ Phiên đang chờ / đang có tư vấn viên? ──► chỉ lưu tin, bot KHÔNG trả lời        (D1-09)
  ├─ guardrails.py: injection, rò rỉ dữ liệu, từ khoá cấm ──► câu từ chối chuẩn + log  (D1-07, D1-13)
  │                  ngoài phạm vi ──► câu từ chối chuẩn, không tính lỗi              (D1-08)
  ├─ Kiểm duyệt Gemini (lớp 2) ──► câu từ chối chuẩn + log
  ├─ Hỏi "thuộc bảng nào?" ──► track_advisor: trả lời theo tuổi/năm sinh/cấp học, thiếu thì hỏi lại (D1-06)
  ├─ Cụm đa nghĩa ("điểm thi") ──► hỏi lại kèm lựa chọn                                (D1-06)
  ├─ Khớp FAQ đã duyệt ──► trả lời nguyên văn câu trả lời chuẩn                         (D1-04)
  └─ RAG: Knowledge Base + Gemini ──► trả lời kèm nguồn                                (D1-04/05/10)
        ├─ [NGOAI_PHAM_VI]  → câu từ chối chuẩn
        ├─ [KHONG_TIM_THAY] → báo chưa có thông tin + hướng dẫn gặp tư vấn viên; fail_count +1
        │                     fail_count = 3 → chuyển tư vấn viên (ngoài giờ: mời để lại thông tin)
        └─ [HOI_LAI]        → câu hỏi làm rõ + nút chọn nhanh
```

Câu từ chối chuẩn (theo bảng BA) dùng chung cho mọi trường hợp vi phạm/ngoài phạm vi:
*"Các câu hỏi bạn đưa ra không liên quan đến thông tin của cuộc thi Python Master. Bạn còn câu hỏi gì cần
giải đáp về cuộc thi Python Master không?"*

---

## 9. Bảng truy vết yêu cầu URD → chức năng → test case

| URD | Yêu cầu | Đáp ứng bằng | Test case QC | Test tự động |
|---|---|---|---|---|
| D1-01 | Chat đa kênh, giao diện thân thiện, câu hỏi gợi ý | `widget.js` (Shadow DOM, responsive), FAQ gợi ý, Messenger/Zalo (`channels/`) | TC-CHAT-01…06, TC-INTEGRATION-01…03 | `test_widget_flow_integration`, `test_channels_*` |
| D1-02 | Gửi ảnh JPG/PNG/WEBP ≤ 5MB, preview trước & sau | Kẹp giấy / Ctrl+V, preview + nút ✕, kiểm tra ở client **và** server (đọc nội dung thật, chặn file đổi đuôi) | TC-IMG-01…06 | `test_oversized_image_is_rejected_with_400` |
| D1-03 | Lưu, xem lại, tải lịch sử | Khôi phục phiên, màn Lịch sử, tải .txt / lưu PDF | TC-HIS-01…05 | `test_api_phase3_integration` |
| D1-04 | Trả lời đúng dữ liệu, nhanh | FAQ khớp trực tiếp + RAG, FAQ Excel đã sửa khớp bảng BA | TC-FAQ-01…15, TC-PERF-01…03 | `test_rag_service`, `test_chat_api_integration` |
| D1-05 | Hỗ trợ Python cơ bản, code block | System prompt phạm vi Python; widget hiển thị khối code + nút Sao chép | TC-PYTHON-01…03 | — (phụ thuộc Gemini) |
| D1-06 | Hỏi lại khi thiếu dữ kiện, gợi ý nhanh, tổng hợp câu trả lời | `track_advisor.py`, `disambiguation.py`, marker `[HOI_LAI]` → nút chọn nhanh | TC-CLARIFY-01…03, TC-CONTEXT-01…03 | `test_track_advisor`, `test_disambiguation`, `test_*clarif*` |
| D1-07 | Chặn nội dung nhạy cảm, từ chối ngắn gọn | `guardrails.py` + `data/banned_keywords.txt` + kiểm duyệt Gemini | TC-SAFETY-01 | `test_guardrails` |
| D1-08 | Từ chối ngoài phạm vi (xã hội, lập trình ngôn ngữ khác) | `guardrails.py` + marker `[NGOAI_PHAM_VI]`, không tính vào fail_count | TC-SAFETY-02 | `test_guardrails`, `test_out_of_scope_*` |
| D1-09 | Nút gặp tư vấn viên, tự chuyển sau 3 lần, thông báo, xem lịch sử | `handover.py`, Staff Dashboard, thông báo In-app + trình duyệt + email, ticket ngoài giờ | TC-HANDOVER-01…15 | `test_live_handover_*`, `test_after_hours_*`, `test_handover_unit` |
| D1-10 | Chống bịa (RAG), báo không rõ + hướng dẫn liên hệ | RAG chỉ dùng tri thức đã duyệt; `cannot_answer` luôn kèm hướng dẫn gặp tư vấn viên | TC-RAG-01…03, TC-RAG-08 | `test_cannot_answer_guides_user_to_staff` |
| D1-11 | Cập nhật tri thức không cần code | Trang /admin: FAQ CRUD, upload PDF/Word/TXT, hiệu lực ngay | TC-RAG-04…07, TC-RAG-09 | `test_admin_faq_crud`, `test_ingest_integration` |
| D1-12 | Báo cáo ngày/tuần/tháng, xuất Excel | Tab Báo cáo, `/api/admin/reports/*` | TC-RP-01…04 | `test_reports_summary_and_excel_export` |
| D1-13 | Chống Prompt Injection, rò rỉ KB/System Prompt/dữ liệu người khác | `guardrails.py`; `user_token` bắt buộc cho API đọc hội thoại | TC-SEC-01…06 | `test_guardrails`, `test_injection_*`, polling 403/404 |
| NFR | Responsive, bảo mật, chỉ thu thập PII khi đồng ý | Giao diện mobile toàn màn hình; form ticket bắt buộc tích đồng ý | — | `test_invalid_ticket` |

---

## 10. Hướng dẫn kiểm thử cho QC

**Chuẩn bị:** làm đủ mục 2; mở `/demo`, `/staff`, `/admin` ở 3 tab; mở DevTools (F12) → Network để xem request.
Trang `/demo` có sẵn các nút câu hỏi mẫu gắn mã test case — bấm là câu hỏi được gửi vào widget.

| Nhóm | Cách thực hiện nhanh | Kết quả mong đợi |
|---|---|---|
| TC-CHAT-01/03 | Mở `/demo`, bấm nút chat | Khung chat mở, lời chào + 5 câu hỏi gợi ý |
| TC-CHAT-06 / TC-HIS-02 | Hỏi vài câu → bấm **+** → hỏi tiếp | `conversation_id` mới (Network), không lẫn ngữ cảnh cũ |
| TC-IMG-01/06 | Chọn ảnh PNG 2MB → xem preview → ✕ → chọn lại → gửi | Preview hiện/ẩn đúng, ảnh hiện trong khung chat |
| TC-IMG-03/04 | Ảnh 5.01MB / đúng 5MB (tạo bằng lệnh dưới) | 5.01MB bị từ chối "Dung lượng ảnh vượt quá giới hạn 5MB"; 5MB được nhận |
| TC-IMG-05 | Chọn PDF/EXE; file `.txt` đổi tên thành `.png` | Báo "Định dạng file không được hỗ trợ" (file giả bị server chặn, HTTP 400) |
| TC-HIS-03/04 | Biểu tượng đồng hồ; biểu tượng tải xuống | Danh sách phiên; file `.txt` và bản in PDF đúng thứ tự tin |
| TC-CLARIFY-01…03 | Bấm "Em thuộc bảng nào vậy?" → bấm nút gợi ý hoặc gõ "Em 15 tuổi" | Hỏi lại kèm 2 nút; sau đó trả lời **Bảng A** |
| TC-SAFETY / TC-SEC | Bấm các nút nhóm "An toàn & ngoài phạm vi" | Câu từ chối chuẩn; bảng `violation_logs` có bản ghi (trừ câu ngoài phạm vi) |
| TC-HANDOVER-01/02 | Bấm **Gặp tư vấn viên** | Trạng thái "Đang chờ tư vấn viên…", phiên hiện ở tab Đang chờ của `/staff` |
| TC-HANDOVER-03…05 | Hỏi 3 câu trong phạm vi nhưng KB không có (Gemini thật) | `fail_count` 1 → 2 → 3, lần 3 `escalated=true`, phiên sang `waiting_agent` |
| TC-HANDOVER-07/11/12/13/15 | `/staff`: mở phiên → Tiếp nhận → trả lời; thí sinh nhắn thêm | Widget nhận tin tư vấn viên trong ~3 giây; bot không tự trả lời |
| TC-HANDOVER-14 | `/staff` → Đóng phiên | Widget báo phiên đã đóng; nhắn tiếp → AI trả lời lại |
| TC-HANDOVER-08/09 | Đặt `SUPPORT_HOURS=00:00-00:01`, `SUPPORT_DAYS=7`, khởi động lại → bấm Gặp tư vấn viên | Báo ngoài giờ + form; gửi form (tích đồng ý) → mã yêu cầu; ticket hiện ở tab Ticket |
| TC-RAG-05…07 | `/admin` → thêm/sửa FAQ → hỏi lại trên widget | Câu trả lời dùng nội dung mới ngay |
| TC-RP-01…04 | `/admin` → Báo cáo → Xem → Xuất Excel | Số liệu khớp, file Excel 2 sheet |
| TC-API-* | Postman: import `postman/*.json`, thư mục **6. Widget, ngoài giờ & Quản trị** cho API mới | Đúng status/thông báo như mục 11 |

Tạo ảnh kiểm thử biên dung lượng (PowerShell hoặc bash đều chạy được):

```powershell
python -c "from PIL import Image; import io; b=io.BytesIO(); Image.new('RGB',(64,64),'red').save(b,'PNG'); d=b.getvalue(); open('anh_5MB.png','wb').write(d+b'\0'*(5*1024*1024-len(d))); open('anh_5.01MB.png','wb').write(d+b'\0'*(int(5.01*1024*1024)-len(d)))"
python -c "open('gia_anh.png','w').write('day khong phai anh')"
```

---

## 11. API mới

Tất cả API `/api/chat*` cần `site_key` (body hoặc header `X-Site-Key`).

| Phương thức & đường dẫn | Mô tả | Lỗi chính |
|---|---|---|
| `GET /api/chat/conversations/<id>/messages?user_id=&after_id=` | Tin nhắn của phiên (widget khôi phục phiên, nhận tin tư vấn viên). Header `X-User-Token` bắt buộc | 403 thiếu/sai token, 404 phiên không thuộc người dùng |
| `POST /api/chat/tickets` | Để lại thông tin ngoài giờ: `user_id, conversation_id, full_name, email/phone, content, consent=true` | 400 thiếu đồng ý / dữ liệu sai, 404 phiên không thuộc người dùng |
| `POST /api/chat/request-agent` *(mở rộng)* | Trả thêm `handover_mode` (`live` / `after_hours`), `after_hours`, `conversation_status`; bấm lại không tạo thông báo trùng | 400 thiếu `conversation_id` |
| `GET /api/staff/conversations?scope=waiting\|mine\|open` *(mở rộng)* | Hàng chờ / phiên của tôi / cả hai | 403 không phải staff |
| `POST /api/staff/conversations/<id>/claim` *(siết lại)* | Chỉ nhận phiên đang chờ | 409 phiên không ở trạng thái chờ |
| `GET /api/staff/tickets?status=` , `POST /api/staff/tickets/<id>/status` | Danh sách / cập nhật ticket | 400 status sai, 404 |
| `GET/POST /api/admin/faqs`, `PUT/DELETE /api/admin/faqs/<id>` | Quản trị FAQ (header `X-Admin-Key`) | 400 thiếu trường, 401 sai khoá, 404 |
| `GET /api/admin/reports/summary?period=&from=&to=` | Thống kê theo ngày/tuần/tháng | 400 ngày sai định dạng |
| `GET /api/admin/reports/unanswered.xlsx?from=&to=` | Excel câu hỏi chưa trả lời / chuyển tư vấn viên | 400 |

Các response chat có thêm: `answer_status` (`faq`, `answered`, `clarify`, `cannot_answer`, `out_of_scope`,
`blocked`), `suggestions` + `clarification_question` (khi hỏi lại), `last_message_id` (mốc polling),
`intent`. `/api/chat/init` trả thêm `support` (`hours`, `available`).

Thay đổi hành vi so với bản trước (QC lưu ý khi so sánh kết quả cũ):
- Ảnh > 6MB bị chặn sớm nay trả **400** "Dung lượng ảnh vượt quá giới hạn 5MB" (trước là 413) — khớp TC-IMG-03.
- Mọi câu vi phạm/ngoài phạm vi trả **câu từ chối chuẩn** của BA thay vì nhiều câu khác nhau.
- Ngày giờ trong JSON dạng `YYYY-MM-DD HH:MM:SS` (trước là định dạng HTTP "…GMT").

---

## 12. Chạy test tự động

```bash
# Test đơn vị (không cần MySQL/Gemini)
pytest

# Kèm test tích hợp trên MySQL thật (tự tạo và xoá database gemini_chat_db_test)
PM_TEST_MYSQL=1 DB_PASSWORD=<mật khẩu> pytest          # PowerShell: $env:PM_TEST_MYSQL=1; pytest
```

Kết quả tại thời điểm bàn giao: **185 test pass** (136 đơn vị + 49 tích hợp MySQL 8.0). Luồng giao diện
(widget → handover → Staff Dashboard → ticket ngoài giờ → admin/báo cáo) đã được chạy tự động bằng
Playwright trên Chromium, desktop 1280px và mobile 390px, không có lỗi JavaScript.

Đo độ chính xác định tuyến câu hỏi (mục tiêu URD ≥ 90%, cần Gemini thật): `python evaluate_chatbot.py`.

---

## 13. Xử lý sự cố thường gặp

| Hiện tượng | Nguyên nhân / cách xử lý |
|---|---|
| Không thấy nút chat | Console báo thiếu `data-site-key`, hoặc sai URL `widget.js` (mở thử URL trên trình duyệt) |
| Mọi tin nhắn báo "site_key không hợp lệ" | `site_key` chưa có trong bảng `sites` hoặc `is_active = 0` |
| "Domain không được phép" (403) | Thêm domain (kèm cổng, vd `localhost:8000`) vào `sites.allowed_domain` |
| Không xem được lịch sử / không nhận tin tư vấn viên | Chưa đặt `APP_SECRET_KEY` ≥ 32 ký tự trong `.env` |
| Widget báo "Không kết nối được máy chủ" | Backend chưa chạy hoặc sai `data-api-base` |
| Trả lời "hệ thống AI đang quá tải" | Gemini hết quota/quá tải (429/503) — hệ thống đã tự thử model dự phòng; xem log server |
| `/admin` báo "API quản trị chưa được bật" | Chưa đặt `ADMIN_API_KEY` (≥ 16 ký tự) |
| `/staff` báo "không có quyền tư vấn viên" | Mã nhập chưa có role `staff` — chạy `python create_staff.py <username>` |
| Lưu PDF không mở cửa sổ | Trình duyệt chặn popup — cho phép popup cho trang |
| Không có email thông báo | Kiểm tra `SMTP_*`, `STAFF_NOTIFY_EMAILS`; lỗi gửi ghi trong log `pmaster.notifier` |

---

## 14. Giới hạn đã biết

- **Tư vấn viên chưa có mật khẩu:** Staff API xác thực bằng `staff_id` + kiểm tra role (như bản trước). Chỉ dùng
  `/staff` trong mạng nội bộ cho tới khi bổ sung đăng nhập thật.
- **PDF lịch sử** tạo bằng hộp thoại in của trình duyệt (đích "Lưu dưới dạng PDF"), không tạo file PDF phía server.
- **Tin tư vấn viên đến widget qua polling 3 giây** (không dùng WebSocket) — đơn giản, chạy được sau mọi proxy.
- **Mã hoá dữ liệu hội thoại khi lưu trữ** (NFR) cần cấu hình ở tầng hạ tầng (HTTPS, mã hoá ổ đĩa/MySQL TDE).
- **Chất lượng câu trả lời AI** (TC-FAQ, TC-PYTHON, TC-RAG) phụ thuộc Gemini và nội dung Knowledge Base; các
  lớp tất định (FAQ, xác định bảng, guardrails, handover) cho kết quả lặp lại được.
- **Zalo OA / Facebook Messenger** cần tài khoản và webhook thật để kiểm thử (xem `GIAI_DOAN_4_DA_KENH_MESSENGER_ZALO.md`).
