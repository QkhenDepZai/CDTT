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
