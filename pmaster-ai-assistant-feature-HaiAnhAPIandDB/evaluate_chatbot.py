"""
Đo độ chính xác định tuyến câu hỏi của chatbot (mục tiêu URD: >= 90%).

    python evaluate_chatbot.py                       # mô phỏng đúng luồng thật
    python evaluate_chatbot.py --mode retrieval      # chỉ đo Knowledge Base (rẻ hơn)
    python evaluate_chatbot.py --file eval/bo_cau_hoi_cua_btc.csv

File câu hỏi (CSV, UTF-8): question,expected,note
    expected = "clarify"  -> chatbot phải HỎI LẠI (câu mơ hồ)
    expected = tên Intent FAQ (cột Intent trong danh_sach_faq.xlsx); nhiều đáp án
               chấp nhận được thì ngăn cách bằng "|".

Chế độ "pipeline" đi đúng thứ tự như chat_service.process_message:
    khử nhập nhằng -> (nếu không đa nghĩa) FAQ matcher -> truy vấn Knowledge Base
và chấm ĐÚNG khi chủ đề được chọn trùng expected. Đây là thước đo "chatbot có
lấy đúng tri thức không" - điều kiện cần để câu trả lời đúng và không bịa.
Kết quả chi tiết ghi ra eval/ket_qua_danh_gia.csv.

Cần: MySQL đã sync-faq, GEMINI_API_KEY hợp lệ (embedding + phân loại FAQ).
"""
import argparse
import csv
import logging
import os
import sys
import time
from collections import defaultdict

import disambiguation
from database import get_db_connection

DEFAULT_FILE = os.path.join("eval", "bo_cau_hoi_danh_gia.csv")
RESULT_FILE = os.path.join("eval", "ket_qua_danh_gia.csv")


def _chunk_topic(chunk):
    metadata = chunk.metadata or {}
    return (metadata.get("intent") or chunk.title or "").strip()


def evaluate_question(cursor, retriever, question, mode):
    """Trả về (chủ đề dự đoán, top-3 chủ đề từ KB, đường đi)."""
    resolution = disambiguation.resolve(question)
    if resolution.needs_clarification:
        return "clarify", [], "hỏi lại"

    if mode == "pipeline" and not resolution.applied:
        from faq_matcher import find_best_faq_match
        faq_row = find_best_faq_match(cursor, question)
        if faq_row:
            return (faq_row.get("intent") or "").strip(), [], "FAQ khớp trực tiếp"

    chunks = retriever.search(resolution.retrieval_query or question, top_k=3, min_score=0.0)
    topics = [_chunk_topic(chunk) for chunk in chunks]
    path = "RAG" + (f" (khử nhập nhằng: {resolution.sense.label})" if resolution.sense else "")
    return (topics[0] if topics else "(không tìm thấy)"), topics, path


def main():
    parser = argparse.ArgumentParser(description="Đo độ chính xác chatbot trên bộ câu hỏi")
    parser.add_argument("--file", default=DEFAULT_FILE)
    parser.add_argument("--mode", choices=["pipeline", "retrieval"], default="pipeline")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Nghỉ giữa các câu (giây) để không vượt quota Gemini")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)

    with open(args.file, encoding="utf-8-sig", newline="") as handle:
        cases = [row for row in csv.DictReader(handle) if (row.get("question") or "").strip()]
    if not cases:
        print(f"Không có câu hỏi nào trong {args.file}")
        return 1

    from knowledge.retriever import get_retriever
    retriever = get_retriever()
    connection = get_db_connection()
    results, by_group = [], defaultdict(lambda: [0, 0])
    top3_hits = top3_total = 0

    try:
        with connection.cursor() as cursor:
            for index, case in enumerate(cases, start=1):
                question = case["question"].strip()
                expected = {e.strip() for e in case["expected"].split("|") if e.strip()}
                try:
                    predicted, top3, path = evaluate_question(cursor, retriever, question, args.mode)
                except Exception as exc:  # noqa: BLE001 - 1 câu lỗi không dừng cả bộ đánh giá
                    predicted, top3, path = f"LỖI: {type(exc).__name__}", [], "lỗi"
                correct = predicted in expected
                if top3 and "clarify" not in expected:
                    top3_total += 1
                    top3_hits += any(topic in expected for topic in top3)

                group = "Câu bẫy" if "bẫy" in (case.get("note") or "") else "Câu thường"
                by_group[group][0] += correct
                by_group[group][1] += 1
                results.append({"question": question, "expected": case["expected"],
                                "predicted": predicted, "correct": "ĐÚNG" if correct else "SAI",
                                "path": path, "top3": " | ".join(top3)})
                mark = "✓" if correct else "✗"
                print(f"[{index:>3}/{len(cases)}] {mark} {question[:55]:<55} -> {predicted}")
                time.sleep(args.delay)
    finally:
        connection.close()

    total_correct = sum(1 for r in results if r["correct"] == "ĐÚNG")
    accuracy = 100.0 * total_correct / len(results)
    print("\n" + "=" * 70)
    print(f"ĐỘ CHÍNH XÁC ({args.mode}): {total_correct}/{len(results)} = {accuracy:.1f}%")
    for group, (ok, total) in sorted(by_group.items()):
        print(f"  - {group:<11}: {ok}/{total} = {100.0 * ok / total:.1f}%")
    if top3_total:
        print(f"  - Đáp án đúng nằm trong top-3 tài liệu truy xuất: "
              f"{top3_hits}/{top3_total} = {100.0 * top3_hits / top3_total:.1f}%")
    print("Mục tiêu URD: >= 90%  ->", "ĐẠT" if accuracy >= 90 else "CHƯA ĐẠT")

    failures = [r for r in results if r["correct"] == "SAI"]
    if failures:
        print("\nCác câu SAI (xem cột path/top3 trong file kết quả để biết vì sao):")
        for r in failures:
            print(f"  ✗ {r['question']}\n      mong đợi: {r['expected']}\n      nhận được: {r['predicted']}"
                  f"  [{r['path']}]")

    os.makedirs(os.path.dirname(RESULT_FILE), exist_ok=True)
    with open(RESULT_FILE, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    print(f"\nChi tiết: {RESULT_FILE} (mở bằng Excel)")
    return 0 if accuracy >= 90 else 2


if __name__ == "__main__":
    sys.exit(main())
