import re
import unicodedata

from gemini_client import classify_faq_intent
from database import get_faq_by_id

VN_STOPWORDS = {
    "là", "của", "và", "các", "những", "một", "này", "đó", "sao", "như",
    "thế", "nào", "gì", "cho", "tôi", "với", "theo", "trong", "trên",
    "dưới", "được", "có", "không", "sẽ", "đã", "đang", "về", "cụ", "thể",
    "thông", "tin", "bạn", "mình", "ạ", "nhé", "vậy", "hãy", "làm", "ơn",
    "giúp", "xin", "nếu", "thì", "mà", "ra", "vào", "lên", "xuống", "rồi",
    "chưa", "hay", "hoặc", "khi", "đâu", "ai", "bao", "nhiêu", "mấy",
    "kia", "ấy", "để", "còn", "lại", "cũng", "rất", "quá", "hơn", "tại",
    "vì", "nên", "phải", "cần", "muốn", "biết", "xem", "giờ", "đây", "kìa",
    "chỗ", "nơi", "nhất", "luôn", "chỉ", "mọi", "tất", "cả",
}

# Ngưỡng cho nhánh match theo tỷ lệ % keyword của USER QUESTION - dùng cho câu
# hỏi gần như y hệt 1 câu hỏi mẫu FAQ (rẻ, không cần gọi Gemini). Đây là nhánh
# thứ 2 trong find_best_faq_match(); nhánh strong-phrase chạy trước.
FAQ_MIN_MATCH_RATIO = 0.9
FAQ_MIN_MATCHED_KEYWORDS = 3

# LƯU Ý (Hướng B - QC_D1 TC-FAQ-02/03): FAQ_SOFT_SCORE_THRESHOLD KHÔNG còn
# được dùng để tự động trả lời trực tiếp nữa - nhánh "ngưỡng điểm mềm" dựa
# thuần từ khoá đã bị thay bằng gemini_client.classify_faq_intent() (phân
# loại ngữ nghĩa) trong find_best_faq_match(), vì ngưỡng điểm thuần từ khoá
# từng gây match sai (không hiểu paraphrase) hoặc bỏ sót câu hỏi đúng (không
# trùng từ khoá dù nghĩa giống hệt). Giữ lại hằng số này chỉ để tương thích
# ngược nếu chỗ khác còn tham chiếu tới, KHÔNG dùng trong luồng quyết định nữa.
FAQ_SOFT_SCORE_THRESHOLD = 0.22

# Các cụm nghiệp vụ có tính phân biệt cao, xác định ĐÚNG CHỦ ĐỀ người dùng
# đang hỏi (lịch thi, kết quả thi, lệ phí...). Nếu cụm này khớp cả 2 phía
# (câu hỏi người dùng + FAQ), gần như chắc chắn đúng chủ đề -> coi là match mạnh.
#
# LƯU Ý: đã bỏ "python master" khỏi danh sách này (bug TC-FAQ-01 phát hiện qua
# QC test case). "python master" là TÊN CUỘC THI, xuất hiện ở gần như MỌI FAQ
# trong hệ thống -> hoàn toàn không có tính phân biệt chủ đề. Để nó trong
# STRONG_TOPIC_PHRASES khiến bất kỳ câu hỏi nào nhắc "Python Master" đều được
# cộng bonus 0.45 cho BẤT KỲ FAQ nào khác cũng nhắc tới cụm này (tức gần như
# toàn bộ 44 FAQ) -> dẫn tới match sai chủ đề (ví dụ "Python Master có mấy
# bảng thi?" bị trả lời nhầm sang FAQ "Đề thi mẫu").
STRONG_TOPIC_PHRASES = {
    "lịch thi",
    "ca thi",
    "phòng thi",
    "số báo danh",
    "đề thi mẫu",
    "kết quả thi",
    "khiếu nại kết quả",
    "đăng ký dự thi",
    "cách đăng ký",
    "lệ phí thi",
    "chứng chỉ",
    "cơ cấu giải thưởng",
    "python prep series",
}

# "bảng a" / "bảng b" là cụm PHÂN BIỆT BẢNG THI, không phải cụm chủ đề. Mục
# đích ban đầu: giúp phân biệt 2 FAQ cùng chủ đề nhưng khác bảng (ví dụ "Nội
# dung Bảng A" vs "Nội dung Bảng B" - xem case đã sửa trước đó). KHÔNG được
# coi ngang hàng với STRONG_TOPIC_PHRASES ở trên, vì rất nhiều FAQ thuộc CHỦ
# ĐỀ KHÁC (hình thức thi, vòng chung kết...) cũng nhắc tới "Bảng A"/"Bảng B"
# như 1 ví dụ minh hoạ trong câu hỏi mẫu (ví dụ: "Bảng A thi bao nhiêu phút?"
# nằm trong FAQ "Hình thức vòng loại", không phải FAQ về lịch thi hay nội
# dung thi). Nếu gộp chung với STRONG_TOPIC_PHRASES, bất kỳ câu hỏi nào của
# user có nhắc "bảng A/B" đều có thể bị match nhầm sang các FAQ KHÁC CHỦ ĐỀ
# chỉ vì chúng tình cờ cũng nhắc tới bảng đó (bug thực tế đã gặp: hỏi "lịch
# thi bảng A" bị trả lời nhầm sang "Hình thức vòng loại" vì FAQ đó có câu hỏi
# mẫu "Bảng A thi bao nhiêu phút?").
TRACK_QUALIFIER_PHRASES = {
    "bảng a",
    "bảng b",
}

# Giữ lại tập hợp đầy đủ để tương thích các chỗ khác (nếu có) từng dùng
# STRONG_PHRASES; nội bộ module này KHÔNG dùng biến này để tính điểm nữa, xem
# _score_candidate() bên dưới.
STRONG_PHRASES = STRONG_TOPIC_PHRASES | TRACK_QUALIFIER_PHRASES

# Các cụm từ ghép dễ bị hiểu sai nghĩa nếu tách rời từng từ đơn. Ví dụ "địa điểm"
# (vị trí) và "điểm thi"/"điểm số" (điểm số) đều chứa từ "điểm" nhưng nghĩa khác
# hẳn nhau - nếu tách rời, câu hỏi về ĐIỂM SỐ dễ bị match nhầm sang FAQ về ĐỊA ĐIỂM.
# Gộp các cụm này thành 1 token duy nhất (nối bằng "_") TRƯỚC KHI tách từ, để giữ
# đúng ngữ cảnh của cụm từ thay vì để từng từ đơn lẻ gây nhiễu.
COMPOUND_TERMS = {
    "địa điểm": "địa_điểm",
}


def normalize_text(text: str) -> str:
    """Chuẩn hóa khoảng trắng/ký tự để so khớp cụm từ ổn định hơn."""
    text = unicodedata.normalize("NFC", str(text or "")).lower().strip()
    text = re.sub(r"[^\w\sÀ-ỹ]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    # Gộp các cụm từ ghép nhạy cảm trước khi tách từ (xem COMPOUND_TERMS ở trên)
    for phrase, merged in COMPOUND_TERMS.items():
        text = text.replace(phrase, merged)
    return text


def extract_keywords(text: str):
    text = normalize_text(text)
    tokens = [t for t in text.split() if t]
    return [t for t in tokens if t not in VN_STOPWORDS]


def keyword_match_ratio(user_keywords, faq_question: str) -> float:
    if not user_keywords:
        return 0.0
    faq_keywords = set(extract_keywords(faq_question))
    matched = sum(1 for kw in user_keywords if kw in faq_keywords)
    return matched / len(user_keywords)


def _row_text(row) -> str:
    """Gộp các trường FAQ quan trọng để tìm theo intent/context, không chỉ question mẫu."""
    return normalize_text(" ".join([
        str(row.get("cau_hoi_mau") or ""),
        str(row.get("intent") or ""),
        str(row.get("tinh_huong") or ""),
        str(row.get("xu_ly_tinh_huong") or ""),
    ]))


def _topic_phrase_overlap(user_message: str, row) -> bool:
    """True nếu có ít nhất 1 cụm CHỦ ĐỀ (STRONG_TOPIC_PHRASES) khớp cả câu hỏi
    người dùng lẫn nội dung FAQ. Đây là tín hiệu match mạnh, đáng tin cậy vì
    các cụm này chỉ xuất hiện ở đúng FAQ liên quan tới chủ đề đó."""
    user_text = normalize_text(user_message)
    candidate_text = _row_text(row)
    return any(
        phrase in user_text and phrase in candidate_text
        for phrase in STRONG_TOPIC_PHRASES
    )


def _track_phrase_overlap(user_message: str, row) -> bool:
    """True nếu "bảng a"/"bảng b" khớp cả câu hỏi người dùng lẫn nội dung FAQ.
    CHỈ dùng để phân biệt bảng thi, KHÔNG phải tín hiệu về chủ đề - xem giải
    thích ở TRACK_QUALIFIER_PHRASES phía trên."""
    user_text = normalize_text(user_message)
    candidate_text = _row_text(row)
    return any(
        phrase in user_text and phrase in candidate_text
        for phrase in TRACK_QUALIFIER_PHRASES
    )


def _strong_phrase_overlap(user_message: str, row) -> bool:
    """True nếu khớp cụm chủ đề HOẶC cụm phân biệt bảng thi (dùng cho nhánh
    trả lời trực tiếp trong find_best_faq_match - vẫn giữ hành vi rộng như cũ
    ở bước NÀY, vì candidates đã được sắp xếp đúng theo điểm số ở
    _score_candidate() bên dưới, nơi 2 loại cụm được cân trọng số khác nhau)."""
    return _topic_phrase_overlap(user_message, row) or _track_phrase_overlap(user_message, row)


def _score_candidate(user_message: str, user_keywords, row):
    """Chấm điểm theo cả keyword của user và coverage của FAQ.

    Điểm quan trọng: câu hỏi dài có thêm bối cảnh sẽ không còn bị phạt mạnh
    chỉ vì chứa nhiều từ mà FAQ không cần lặp lại.
    """
    question_text = normalize_text(row.get("cau_hoi_mau") or "")
    combined_text = _row_text(row)

    user_set = set(user_keywords)
    combined_keywords = set(extract_keywords(combined_text))
    question_keywords = set(extract_keywords(question_text))

    matched_user = user_set & combined_keywords
    user_coverage = len(matched_user) / max(1, len(user_set))
    faq_coverage = len(matched_user & question_keywords) / max(1, len(question_keywords))

    # BUG ĐÃ SỬA: bản cũ cộng CÙNG 1 mức bonus (0.45) cho cả match cụm CHỦ ĐỀ
    # (ví dụ "lịch thi") lẫn match cụm PHÂN BIỆT BẢNG THI (ví dụ "bảng a").
    # Hậu quả: 1 FAQ thuộc chủ đề hoàn toàn khác (ví dụ "Hình thức vòng loại")
    # nhưng tình cờ có câu hỏi mẫu nhắc "Bảng A" (ví dụ "Bảng A thi bao nhiêu
    # phút?") được cộng điểm ngang với FAQ đúng chủ đề "Thông báo lịch thi",
    # dẫn tới trả lời sai chủ đề khi user hỏi "lịch thi bảng A".
    # Sửa: match cụm CHỦ ĐỀ vẫn giữ bonus cao (0.45, tín hiệu đáng tin cậy);
    # match cụm BẢNG THI (không tự nói lên chủ đề) chỉ được bonus thấp hơn
    # (0.15) - đủ để làm "tie-breaker" phân biệt Bảng A/B khi 2 FAQ cùng chủ
    # đề (như "Nội dung Bảng A" vs "Nội dung Bảng B"), nhưng không đủ để một
    # FAQ sai chủ đề vượt qua FAQ đúng chủ đề chỉ vì nhắc tới đúng bảng.
    if _topic_phrase_overlap(user_message, row):
        phrase_bonus = 0.45
    elif _track_phrase_overlap(user_message, row):
        phrase_bonus = 0.15
    else:
        phrase_bonus = 0.0

    # Ưu tiên match cụm nghiệp vụ; sau đó đến keyword coverage.
    score = min(1.0, 0.45 * user_coverage + 0.40 * faq_coverage + phrase_bonus)
    matched_count = len(matched_user)
    return score, matched_count


# Số ứng viên tối thiểu lấy từ SQL TRƯỚC KHI Python chấm điểm/xếp hạng lại.
# QUAN TRỌNG: đây là nguyên nhân của bug "hỏi Bảng A trả lời Bảng B" - xem
# giải thích trong _retrieve_candidates() bên dưới.
SQL_CANDIDATE_POOL_SIZE = 30


def _retrieve_candidates(cursor, user_message: str, limit=5):
    user_keywords = extract_keywords(user_message)
    if not user_keywords:
        return []

    boolean_query = " ".join(f"{kw}*" for kw in user_keywords)
    # Lấy 1 tập ứng viên RỘNG hơn `limit` cuối cùng cần trả về (SQL_CANDIDATE_POOL_SIZE),
    # để bước chấm điểm bằng Python (_score_candidate/_strong_phrase_overlap) có
    # đủ dữ liệu để so sánh và chọn đúng FAQ điểm cao nhất.
    #
    # BUG ĐÃ SỬA: bản cũ dùng "LIMIT %s" (limit=5 hoặc 10) NGAY TRONG SQL, và
    # KHÔNG có ORDER BY theo độ liên quan. Trong MySQL, 1 câu MATCH...AGAINST
    # ở chế độ BOOLEAN MODE KHÔNG tự sắp xếp theo relevance nếu không có
    # "ORDER BY MATCH(...) DESC" - MySQL trả về các dòng khớp theo thứ tự nội
    # bộ tuỳ ý (thường gần với thứ tự index/lưu trữ), KHÔNG phải dòng liên
    # quan nhất trước. Với từ khóa rất phổ biến như "thi", "bảng" (khớp ở
    # hàng trăm dòng FAQ), LIMIT 10 có thể cắt mất đúng FAQ liên quan nhất
    # ("Nội dung Bảng A") ra khỏi tập ứng viên TRƯỚC KHI Python kịp chấm điểm
    # và so sánh với FAQ sai ("Nội dung Bảng B") - dẫn đến trả lời nhầm bảng.
    # Thêm ORDER BY MATCH(...) DESC để MySQL ưu tiên trả về các dòng liên
    # quan nhất trước khi bị LIMIT cắt bớt, đồng thời tăng LIMIT lên
    # SQL_CANDIDATE_POOL_SIZE (30) để có vùng đệm an toàn.
    sql_fetch_limit = max(limit, SQL_CANDIDATE_POOL_SIZE)
    try:
        cursor.execute("""
            SELECT id, intent, tinh_huong, xu_ly_tinh_huong, tra_loi_chuan, cau_hoi_mau
            FROM faqs
            WHERE is_active = 1
              AND MATCH(cau_hoi_mau) AGAINST(%s IN BOOLEAN MODE)
            ORDER BY MATCH(cau_hoi_mau) AGAINST(%s IN BOOLEAN MODE) DESC
            LIMIT %s
        """, (boolean_query, boolean_query, sql_fetch_limit))
        candidates = cursor.fetchall()

        # Full-text chỉ search cau_hoi_mau; nếu user dùng cụm nằm ở intent,
        # ví dụ 'lịch thi', vẫn bổ sung ứng viên từ Intent/Tình huống.
        if not candidates:
            cursor.execute("""
                SELECT id, intent, tinh_huong, xu_ly_tinh_huong, tra_loi_chuan, cau_hoi_mau
                FROM faqs
                WHERE is_active = 1
            """)
            candidates = cursor.fetchall()
    except Exception as e:
        print(f"Lỗi truy vấn Full-Text Search: {e}")
        cursor.execute("""
            SELECT id, intent, tinh_huong, xu_ly_tinh_huong, tra_loi_chuan, cau_hoi_mau
            FROM faqs
            WHERE is_active = 1
        """)
        candidates = cursor.fetchall()

    scored = []
    for row in candidates:
        score, matched_count = _score_candidate(user_message, user_keywords, row)
        scored.append((score, matched_count, row))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [row for _, _, row in scored[:limit]]


def find_faq_candidates(cursor, user_message: str, limit=3):
    """Rút top FAQ liên quan để dùng làm retrieval context cho Gemini."""
    return _retrieve_candidates(cursor, user_message, limit=limit)


def _fetch_all_active_faqs_brief(cursor):
    """Lấy toàn bộ FAQ đang active, chỉ id/intent + 1 dòng câu hỏi mẫu đại
    diện - dùng cho classify_faq_intent() (gemini_client.py). Lấy TOÀN BỘ,
    không lọc theo từ khoá, vì lọc theo từ khoá trước là đúng nguyên nhân bỏ
    sót FAQ đúng trong TC-FAQ-02 (câu hỏi paraphrase không trùng từ khoá nào)."""
    cursor.execute(
        "SELECT id, intent, cau_hoi_mau FROM faqs WHERE is_active = 1"
    )
    rows = cursor.fetchall()
    catalog = []
    for row in rows:
        cau_hoi_mau = str(row.get("cau_hoi_mau") or "").strip()
        # Chỉ lấy dòng đầu tiên (câu hỏi mẫu đại diện) để prompt gọn, tránh
        # gửi hết mọi câu hỏi mẫu của 44 FAQ (tốn token không cần thiết).
        first_line = next((line.strip("- ").strip() for line in cau_hoi_mau.splitlines() if line.strip()), "")
        catalog.append({
            "id": row.get("id"),
            "intent": row.get("intent") or "",
            "sample_question": first_line,
        })
    return catalog


def find_best_faq_match(cursor, user_message: str):
    """Tìm FAQ đủ tin cậy để trả lời trực tiếp.

    Có thêm strong-phrase matching để các câu hỏi dài như
    'lịch thi cụ thể của Mùa giải Đấu trường lập trình Python Master 2026'
    vẫn khớp FAQ 'Thông báo lịch thi'.

    THAY ĐỔI (Hướng B - QC_D1, TC-FAQ-02/03): bỏ nhánh "ngưỡng điểm mềm"
    (matched_count/score threshold) dựa thuần vào từ khoá - đã chứng minh
    dễ match sai (TC-FAQ-01 dạng cũ) hoặc bỏ sót câu hỏi paraphrase không
    trùng từ khoá (TC-FAQ-02: "Chưa từng học lập trình có được đăng ký
    không?" vs FAQ đúng "Chưa biết nhiều về Python có được thi không?" -
    trùng 0 từ khoá dù nghĩa gần như giống hệt). Thay bằng 1 lệnh gọi Gemini
    phân loại NGỮ NGHĨA (classify_faq_intent) khi không có match chắc chắn
    theo cụm chủ đề (nhánh 1) hay theo tỉ lệ khớp gần tuyệt đối (nhánh 2,
    giữ lại vì rẻ - không cần gọi Gemini cho câu hỏi gần như y hệt mẫu).

    Đánh đổi: các câu hỏi không khớp thẳng cụm chủ đề/tỉ lệ gần tuyệt đối sẽ
    tốn thêm 1 lệnh gọi Gemini (model rẻ, output rất ngắn) trước khi rơi
    xuống luồng Gemini trả lời tự do - chấp nhận theo đúng yêu cầu ưu tiên
    độ chính xác đã thống nhất.
    """
    user_keywords = extract_keywords(user_message)
    if not user_keywords:
        return None

    candidates = _retrieve_candidates(cursor, user_message, limit=10)

    # Nhánh 1: khớp cụm chủ đề - tín hiệu rẻ, đáng tin cậy, giữ nguyên.
    for row in candidates:
        if _strong_phrase_overlap(user_message, row):
            return row

    # Nhánh 2: câu hỏi gần như y hệt 1 câu hỏi mẫu (tỉ lệ khớp từ khoá rất
    # cao) - rẻ, không cần gọi Gemini cho trường hợp quá rõ ràng này.
    for row in candidates:
        ratio = keyword_match_ratio(user_keywords, row.get("cau_hoi_mau") or "")
        required_matches = min(FAQ_MIN_MATCHED_KEYWORDS, len(user_keywords))
        _, matched_count = _score_candidate(user_message, user_keywords, row)
        if ratio >= FAQ_MIN_MATCH_RATIO and matched_count >= required_matches:
            return row

    # Nhánh 3 (mới): không có gì chắc chắn theo từ khoá -> hỏi Gemini phân
    # loại ngữ nghĩa trên TOÀN BỘ danh mục FAQ (không giới hạn ở `candidates`
    # vì candidates đã bị lọc theo từ khoá từ bước SQL, có thể thiếu đúng FAQ
    # cần tìm - xem TC-FAQ-02).
    try:
        catalog = _fetch_all_active_faqs_brief(cursor)
        matched_faq_id = classify_faq_intent(user_message, catalog)
        if matched_faq_id:
            faq_row = get_faq_by_id(cursor, matched_faq_id)
            if faq_row:
                return faq_row
    except Exception:  # noqa: BLE001
        # Lỗi ở bước phân loại phụ này KHÔNG được làm gián đoạn luồng chat
        # chính - rơi xuống trả về None như bình thường (Gemini tự trả lời
        # tự do kèm retrieval_context từ `candidates` bên trên).
        pass

    return None
