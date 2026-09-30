import os
from dotenv import load_dotenv

# Nạp các biến trong file .env vào bộ nhớ hệ thống
load_dotenv()

DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME', 'gemini_chat_db'),
}

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# ============================================================
# MODULE GEMINI (model, retry, thinking, token)
# ============================================================
# Model chính dùng để trả lời câu hỏi (chat). Có thể đổi qua biến môi trường
# GEMINI_MODEL_NAME mà không cần sửa code / deploy lại.
#
# Đã kiểm tra tài liệu chính thức Gemini API (ai.google.dev/gemini-api/docs/models,
# cập nhật 2026-09-24):
#   - "gemini-3.5-flash" (model cũ đang dùng) VẪN còn hoạt động, nhưng đã được
#     Google xếp vào nhóm "legacy" trong dòng Gemini 3.5 (không sai, chỉ không
#     còn là lựa chọn tối ưu cho use-case mới).
#   - Với chatbot hỏi-đáp FAQ + RAG như Python Master 2026 (câu hỏi ngắn, cần
#     trả lời NHANH, không cần suy luận nhiều bước), Google khuyến nghị dùng
#     nhóm "Flash-Lite": nhanh nhất, rẻ nhất trong họ Gemini 3.x, vẫn hỗ trợ
#     multimodal (ảnh) + "Thinking" ở mức tối thiểu.
#   - "gemini-2.5-flash" KHÔNG được chọn làm mặc định mới vì Google đang giới
#     hạn quota cho model 2.5 (chỉ ưu tiên các dự án đã dùng từ trước) và
#     khuyến nghị dự án mới chuyển sang 3.5 Flash-Lite / 3.8 Flash.
#
# => Mặc định mới: gemini-3.5-flash-lite (xem AUDIT_REPORT.md mục A để so sánh
#    latency/token/cost/reasoning với gemini-3.1-flash-lite và gemini-3.5-flash).
GEMINI_MODEL_NAME = os.getenv('GEMINI_MODEL_NAME', 'gemini-3.5-flash-lite')

# Model DỰ PHÒNG: khi model chính vẫn báo 503 (quá tải) / 429 (hết quota) sau
# khi đã thử lại, rag_service tự gửi lại câu hỏi bằng model này. Mỗi model có
# hạn mức và tải riêng nên hiếm khi cả 2 cùng quá tải (NFR sẵn sàng 99.9%).
# Để trống = tắt dự phòng.
GEMINI_FALLBACK_MODEL_NAME = os.getenv('GEMINI_FALLBACK_MODEL_NAME', 'gemini-3.5-flash')

# Model dùng riêng cho bước kiểm duyệt (moderation.check_violation). Đây chỉ là
# bước "chấm điểm an toàn" 1 câu hỏi ngắn, không cần model mạnh -> dùng luôn
# Flash-Lite để tối ưu chi phí/latency, tách biến môi trường riêng để có thể
# đổi độc lập với model trả lời chính nếu cần.
GEMINI_MODERATION_MODEL_NAME = os.getenv('GEMINI_MODERATION_MODEL_NAME', 'gemini-3.5-flash-lite')

# Mức độ "thinking" (suy luận nội bộ) của model trước khi trả lời.
# Các giá trị hợp lệ theo tài liệu Gemini 3.x: minimal | low | medium | high.
# Chatbot FAQ không cần suy luận nhiều bước -> "minimal" giúp giảm token/latency
# (gemini-3.5-flash-lite mặc định vốn đã là "minimal", set tường minh ở đây để
# hành vi luôn rõ ràng, không phụ thuộc default có thể đổi ở các bản SDK sau).
GEMINI_THINKING_LEVEL = os.getenv('GEMINI_THINKING_LEVEL', 'minimal')

# Giới hạn cứng số token OUTPUT (bao gồm cả thinking token) cho 1 lần trả lời.
# Câu trả lời FAQ thường ngắn -> chặn model "lan man" gây tốn token.
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv('GEMINI_MAX_OUTPUT_TOKENS', '600'))

# Timeout (ms) cho 1 request gọi Gemini. Nếu quá thời gian này mà chưa có phản
# hồi, SDK sẽ raise lỗi (thường là ServerError/timeout) để tầng retry xử lý,
# thay vì treo request của người dùng vô thời hạn.
GEMINI_TIMEOUT_MS = int(os.getenv('GEMINI_TIMEOUT_MS', '20000'))

# Số lần thử lại tối đa (KHÔNG tính lần gọi đầu tiên) khi gặp lỗi có thể retry
# (429 / 500 / 502 / 503 / 504). Xem giải thích đầy đủ trong gemini_client.py
# vì sao chỉ nên có DUY NHẤT 1 tầng retry (ở tầng app, không để SDK tự retry
# ngầm thêm 1 lần nữa gây "double retry" như lỗi hiện tại của dự án).
GEMINI_MAX_RETRIES = int(os.getenv('GEMINI_MAX_RETRIES', '2'))

# ============================================================
# MODULE CONVERSATION HISTORY (giảm token khi hội thoại dài)
# ============================================================
# Số lượt (turn) GẦN NHẤT được gửi nguyên văn cho Gemini. Các lượt cũ hơn (nếu
# có) sẽ được nén thành 1 đoạn tóm tắt ngắn (xem conversation_memory.py) thay vì
# gửi lại toàn bộ lịch sử -> tránh prompt phình to theo số lượt chat.
# 1 "lượt" ở đây = 1 message (user hoặc model), nên 8 nghĩa là khoảng 4 cặp
# hỏi-đáp gần nhất.
CONVERSATION_RECENT_TURNS = int(os.getenv('CONVERSATION_RECENT_TURNS', '8'))

# Số message CŨ tối thiểu cần có trước khi bắt đầu tạo/tạo lại summary. Tránh
# việc hội thoại vừa dài hơn recent-turns 1-2 tin nhắn đã tốn thêm 1 lần gọi
# Gemini để tóm tắt (không đáng, vì recent-turns đã đủ chứa hết).
CONVERSATION_SUMMARY_MIN_OLD_MESSAGES = int(os.getenv('CONVERSATION_SUMMARY_MIN_OLD_MESSAGES', '4'))

# ============================================================
# MODULE RAG / RETRIEVAL CONTEXT (giảm token nhồi vào prompt)
# ============================================================
# Số FAQ liên quan nhất được lấy làm retrieval context cho MỖI câu hỏi (không
# phải cố định cho cả conversation). Xem faq_matcher.find_faq_candidates().
# Đánh giá trade-off Top 3 / 5 / 8 nằm trong AUDIT_REPORT.md mục 4.
RAG_TOP_K = int(os.getenv('RAG_TOP_K', '3'))

# Cắt bớt độ dài mỗi field của 1 chunk FAQ khi đưa vào retrieval context, để
# một FAQ có phần "xử lý tình huống" quá dài không chiếm hết ngân sách token.
RAG_MAX_CHARS_PER_FIELD = int(os.getenv('RAG_MAX_CHARS_PER_FIELD', '400'))

# ============================================================
# MODULE IMAGE (upload ảnh)
# ============================================================
# Thư mục lưu ảnh vật lý trên server. Có thể đổi qua biến môi trường UPLOAD_FOLDER
# nếu sau này chuyển sang ổ đĩa/volume khác (chưa xử lý Cloud Storage S3/Cloudinary
# trong bản này - xem ghi chú trong image_handler.py để mở rộng sau).
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads/images')
MAX_IMAGE_SIZE_MB = 5

# ============================================================
# MODULE KNOWLEDGE BASE / VECTOR RAG (Giai đoạn 1 - thư mục knowledge/)
# ============================================================
# Model embedding của Google. "gemini-embedding-001" là bản GA, hỗ trợ tham
# số task_type (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY). Nếu đổi sang
# "gemini-embedding-2", knowledge/embedding_service.py tự chuyển sang cách
# ghi task bằng tiền tố trong nội dung (model đó không nhận task_type).
# LƯU Ý: đổi model/số chiều => BẮT BUỘC chạy lại
# `python manage_knowledge.py reindex --all` vì vector cũ không so sánh được.
EMBEDDING_MODEL_NAME = os.getenv('EMBEDDING_MODEL_NAME', 'gemini-embedding-001')

# 768 chiều: chất lượng gần bằng 3072 (nhờ Matryoshka) nhưng nhẹ hơn 4 lần
# về RAM/DB (768 * 4 byte = 3 KB/chunk). Các giá trị khuyến nghị: 768 | 1536 | 3072.
EMBEDDING_DIMENSION = int(os.getenv('EMBEDDING_DIMENSION', '768'))

# Số đoạn văn gửi trong 1 request embed (API giới hạn 100/lần).
EMBEDDING_BATCH_SIZE = int(os.getenv('EMBEDDING_BATCH_SIZE', '50'))
EMBEDDING_TIMEOUT_MS = int(os.getenv('EMBEDDING_TIMEOUT_MS', '60000'))
EMBEDDING_MAX_RETRIES = int(os.getenv('EMBEDDING_MAX_RETRIES', '3'))

# Chunking theo ký tự: ~1000 ký tự tiếng Việt ~ 250-300 token, đủ chứa trọn
# 1 điều khoản thể lệ; overlap 150 ký tự để không mất ngữ cảnh ở ranh giới.
CHUNK_SIZE_CHARS = int(os.getenv('CHUNK_SIZE_CHARS', '1000'))
CHUNK_OVERLAP_CHARS = int(os.getenv('CHUNK_OVERLAP_CHARS', '150'))

# Ngưỡng cosine tối thiểu để 1 chunk được coi là "liên quan". Dưới ngưỡng
# => coi như KHÔNG tìm thấy trong KB -> AI phải báo không rõ (D1-10).
# Nên hiệu chỉnh lại bằng `python manage_knowledge.py search "..."` trên
# bộ câu hỏi thử của Ban tổ chức.
RAG_MIN_SIMILARITY = float(os.getenv('RAG_MIN_SIMILARITY', '0.55'))

# Chu kỳ (giây) mỗi tiến trình kiểm tra KB có thay đổi (upload từ worker
# khác) để nạp lại chỉ mục vector trong RAM.
RAG_INDEX_REFRESH_SECONDS = int(os.getenv('RAG_INDEX_REFRESH_SECONDS', '30'))

KNOWLEDGE_UPLOAD_FOLDER = os.getenv('KNOWLEDGE_UPLOAD_FOLDER', 'uploads/knowledge')
KNOWLEDGE_MAX_FILE_MB = int(os.getenv('KNOWLEDGE_MAX_FILE_MB', '20'))

# Tổng ngân sách ký tự của toàn bộ RETRIEVED DATA ghép vào system prompt
# (~4000 ký tự tiếng Việt ~ 1.100 token). Chunk vượt ngân sách bị bỏ, ưu tiên
# giữ các chunk điểm cao nhất -> prompt không phình to khi tăng RAG_TOP_K.
RAG_MAX_CONTEXT_CHARS = int(os.getenv('RAG_MAX_CONTEXT_CHARS', '4000'))

# Câu hỏi ngắn hơn ngưỡng (số từ) được ghép thêm câu hỏi trước đó của người
# dùng khi truy vấn KB, để câu nối tiếp kiểu "Còn Bảng B thì sao?" vẫn tìm
# đúng tài liệu (xem rag_service._build_retrieval_query).
RAG_FOLLOWUP_MAX_WORDS = int(os.getenv('RAG_FOLLOWUP_MAX_WORDS', '8'))

# ============================================================
# MODULE SERVER (main.py)
# ============================================================
APP_HOST = os.getenv('APP_HOST', '0.0.0.0')
APP_PORT = int(os.getenv('APP_PORT', '5000'))

# ============================================================
# MODULE BẢO MẬT API (Giai đoạn 3)
# ============================================================
# Khoá bí mật ký user_token (HMAC-SHA256). user_token chứng minh người gọi
# đúng là chủ của user_id -> chặn xem trộm lịch sử chat của người khác
# (D1-13). Tạo chuỗi ngẫu nhiên:  python -c "import secrets; print(secrets.token_urlsafe(48))"
# Đổi khoá => mọi user_token cũ mất hiệu lực.
APP_SECRET_KEY = os.getenv('APP_SECRET_KEY', '')

# Khoá cho API quản trị Knowledge Base (/api/knowledge/*), gửi qua header
# X-Admin-Key. Để trống => các API quản trị bị khoá hoàn toàn (fail-closed).
ADMIN_API_KEY = os.getenv('ADMIN_API_KEY', '')

# true => /api/chat* bắt buộc gửi user_token hợp lệ khi truyền user_id có sẵn
# (chống ghi tin nhắn vào phiên của người khác). Mặc định false để widget cũ
# chưa gửi token vẫn chạy; nên bật khi frontend đã lưu user_token.
ENFORCE_USER_TOKEN = os.getenv('ENFORCE_USER_TOKEN', 'false').lower() in ('1', 'true', 'yes')

HISTORY_MAX_PAGE_SIZE = int(os.getenv('HISTORY_MAX_PAGE_SIZE', '100'))

# ============================================================
# MODULE ĐA KÊNH (Giai đoạn 4 - channels/, routes/webhooks.py)
# Kênh chỉ bật khi điền ĐỦ khoá bắt buộc; thiếu khoá => webhook trả 503
# (fail-closed, không bao giờ bỏ qua bước xác thực chữ ký).
# ============================================================
# ---- Facebook Messenger (developers.facebook.com > App > Messenger) ----
FB_PAGE_ACCESS_TOKEN = os.getenv('FB_PAGE_ACCESS_TOKEN', '')
# App Secret: dùng kiểm tra chữ ký X-Hub-Signature-256 của mọi webhook.
FB_APP_SECRET = os.getenv('FB_APP_SECRET', '')
# Chuỗi tự đặt, nhập giống hệt ở ô "Verify token" khi đăng ký webhook.
FB_VERIFY_TOKEN = os.getenv('FB_VERIFY_TOKEN', '')
FB_GRAPH_API_VERSION = os.getenv('FB_GRAPH_API_VERSION', 'v23.0')
# true => tin tư vấn viên gửi kèm tag HUMAN_AGENT (trả lời được tới 7 ngày sau
# tin cuối của người dùng thay vì 24h). Cần Meta duyệt quyền Human Agent.
FB_USE_HUMAN_AGENT_TAG = os.getenv('FB_USE_HUMAN_AGENT_TAG', 'false').lower() in ('1', 'true', 'yes')

# ---- Zalo Official Account (developers.zalo.me > Ứng dụng > Official Account) ----
ZALO_APP_ID = os.getenv('ZALO_APP_ID', '')
# Khoá bí mật của ỨNG DỤNG: dùng khi làm mới access token (header secret_key).
ZALO_APP_SECRET = os.getenv('ZALO_APP_SECRET', '')
# Khoá bí mật của OA ("OA Secret Key" ở mục Webhook): kiểm tra X-ZEvent-Signature.
ZALO_OA_SECRET_KEY = os.getenv('ZALO_OA_SECRET_KEY', '')
# Token khởi tạo lần đầu (lấy qua OAuth v4). Sau đó hệ thống tự làm mới và
# lưu token mới vào bảng channel_tokens; không cần sửa .env nữa.
ZALO_ACCESS_TOKEN = os.getenv('ZALO_ACCESS_TOKEN', '')
ZALO_REFRESH_TOKEN = os.getenv('ZALO_REFRESH_TOKEN', '')
ZALO_OA_ID = os.getenv('ZALO_OA_ID', '')

# Số luồng xử lý tin nhắn đa kênh song song. Webhook trả 200 ngay, việc gọi
# AI chạy nền (Messenger/Zalo sẽ gửi lại nếu không nhận 200 trong vài giây).
CHANNEL_WORKERS = int(os.getenv('CHANNEL_WORKERS', '4'))
CHANNEL_HTTP_TIMEOUT_SECONDS = float(os.getenv('CHANNEL_HTTP_TIMEOUT_SECONDS', '10'))

# ============================================================
# MODULE NGHIỆP VỤ CUỘC THI (track_advisor.py - xác định Bảng A/B, D1-06)
# Nguồn: bảng "Xác định câu hỏi và tình huống" của BA (Bảng A 13-18 tuổi,
# THCS/THPT; Bảng B 19-24 tuổi, ĐH/CĐ). Tuổi tính theo năm: năm thi - năm sinh.
# ============================================================
COMPETITION_YEAR = int(os.getenv('COMPETITION_YEAR', '2026'))
TRACK_A_MIN_AGE = int(os.getenv('TRACK_A_MIN_AGE', '13'))
TRACK_A_MAX_AGE = int(os.getenv('TRACK_A_MAX_AGE', '18'))
TRACK_B_MIN_AGE = int(os.getenv('TRACK_B_MIN_AGE', '19'))
TRACK_B_MAX_AGE = int(os.getenv('TRACK_B_MAX_AGE', '24'))

# Thông tin liên hệ chính thức, chèn vào câu trả lời khi AI không có dữ liệu
# (D1-10) và khi ngoài giờ trực (D1-09).
SUPPORT_CONTACT_TEXT = os.getenv(
    'SUPPORT_CONTACT_TEXT',
    'Email: info@pythonmaster.vn · Hotline: 0347 314 969 / 0354 549 889',
)

# ============================================================
# MODULE HANDOVER (handover.py - D1-09)
# ============================================================
# Giờ trực của tư vấn viên, dạng "HH:MM-HH:MM". Để trống = luôn có người trực
# (mọi yêu cầu chuyển thẳng vào hàng chờ). Ngoài giờ: widget hiện form để lại
# thông tin (support_tickets) thay vì bắt thí sinh chờ vô thời hạn.
SUPPORT_HOURS = os.getenv('SUPPORT_HOURS', '').strip()
# Các ngày làm việc, 1 = Thứ Hai ... 7 = Chủ Nhật, dạng "1-5" hoặc "1,2,3,4,5,6".
SUPPORT_DAYS = os.getenv('SUPPORT_DAYS', '1-7').strip()
# Múi giờ của giờ trực, tính bằng giờ lệch so với UTC (Việt Nam = 7, không có
# giờ mùa hè nên không cần thư viện múi giờ - chạy được cả trên Windows).
SUPPORT_UTC_OFFSET_HOURS = float(os.getenv('SUPPORT_UTC_OFFSET_HOURS', '7'))

# Thông báo email cho nhân viên trực khi có yêu cầu gặp tư vấn viên / ticket
# mới. Để trống SMTP_HOST hoặc STAFF_NOTIFY_EMAILS = tắt email (vẫn có thông
# báo trong Staff Dashboard).
SMTP_HOST = os.getenv('SMTP_HOST', '').strip()
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
SMTP_USE_TLS = os.getenv('SMTP_USE_TLS', 'true').lower() in ('1', 'true', 'yes')
SMTP_FROM = os.getenv('SMTP_FROM', '') or SMTP_USER
STAFF_NOTIFY_EMAILS = [e.strip() for e in os.getenv('STAFF_NOTIFY_EMAILS', '').split(',') if e.strip()]
# Đường dẫn Staff Dashboard ghi trong email thông báo.
STAFF_DASHBOARD_URL = os.getenv('STAFF_DASHBOARD_URL', 'http://localhost:5000/staff')
