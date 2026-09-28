import pytest

import rag_service
from knowledge.retriever import RetrievedChunk
from rag_service import NO_CONTEXT_NOTICE, ImageInput, RagService


def _chunk(chunk_id, content, score=0.8, title="Thể lệ 2026", page=None):
    return RetrievedChunk(chunk_id=chunk_id, document_id=1, title=title, source_type="pdf",
                          content=content, score=score, page_number=page)


class FakeRetriever:
    def __init__(self, chunks=None, error=None):
        self.chunks = chunks or []
        self.error = error
        self.queries = []

    def search(self, query, top_k, min_score):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.chunks[:top_k]


@pytest.fixture()
def gemini(monkeypatch):
    """Thay Gemini bằng bản giả lập, ghi lại context/message đã gửi."""
    calls = {"reply": ("Bảng A thi 90 phút.", "answered")}

    def fake_create_chat(history, retrieval_context=None):
        calls["history"] = history
        calls["context"] = retrieval_context
        return object()

    def fake_send(chat, message):
        calls["message"] = message
        return calls["reply"]

    def fake_send_image(chat, message, data, mime):
        calls["message"] = message
        calls["image"] = (data, mime)
        return calls["reply"]

    monkeypatch.setattr(rag_service, "create_chat", fake_create_chat)
    monkeypatch.setattr(rag_service, "send_message_with_retry", fake_send)
    monkeypatch.setattr(rag_service, "send_message_with_image_retry", fake_send_image)
    return calls


def test_answer_injects_numbered_sources_and_returns_citations(gemini):
    retriever = FakeRetriever([_chunk(11, "Bảng A thi 90 phút.", page=3), _chunk(12, "Bảng B thi 120 phút.")])
    answer = RagService(retriever=retriever, top_k=3).answer("Bảng A thi bao lâu?")

    assert "[Nguồn 1] Thể lệ 2026 (tr.3)\nBảng A thi 90 phút." in gemini["context"]
    assert "[Nguồn 2]" in gemini["context"]
    assert gemini["message"] == "Bảng A thi bao lâu?"
    assert answer.status == "answered"
    assert answer.chunk_ids == [11, 12]
    assert answer.sources_payload()[0] == {
        "title": "Thể lệ 2026", "citation": "Thể lệ 2026 (tr.3)", "score": 0.8, "method": "vector",
    }
    assert "content" not in str(answer.sources_payload())


def test_no_relevant_chunk_sends_explicit_no_data_notice(gemini):
    gemini["reply"] = ("Mình chưa có thông tin này.", "cannot_answer")
    answer = RagService(retriever=FakeRetriever([])).answer("Giải nhất được bao nhiêu tiền?")
    assert gemini["context"] == NO_CONTEXT_NOTICE
    assert answer.status == "cannot_answer"
    assert answer.sources_payload() == []


def test_sources_hidden_when_not_answered_but_kept_for_audit(gemini):
    gemini["reply"] = ("Không rõ.", "cannot_answer")
    answer = RagService(retriever=FakeRetriever([_chunk(5, "abc")])).answer("câu hỏi khó")
    assert answer.sources_payload() == []
    assert answer.chunk_ids == [5]


def test_context_budget_keeps_best_chunks_only(gemini):
    chunks = [_chunk(i, "x" * 300, score=1 - i / 10) for i in range(1, 6)]
    answer = RagService(retriever=FakeRetriever(chunks), top_k=5, max_context_chars=700).answer("q")
    assert answer.chunk_ids == [1, 2]
    assert len(gemini["context"]) <= 700


def test_single_huge_chunk_is_truncated_not_dropped(gemini):
    answer = RagService(retriever=FakeRetriever([_chunk(1, "y" * 5000)]),
                        max_context_chars=500).answer("q")
    assert answer.chunk_ids == [1]
    assert len(gemini["context"]) <= 501


def test_short_followup_question_uses_previous_user_turn_for_retrieval(gemini):
    retriever = FakeRetriever([])
    history = [
        {"role": "user", "parts": [{"text": "Lệ phí thi Bảng A là bao nhiêu?"}]},
        {"role": "model", "parts": [{"text": "Miễn phí."}]},
    ]
    RagService(retriever=retriever).answer("Còn Bảng B?", history)
    assert retriever.queries == ["Lệ phí thi Bảng A là bao nhiêu?\nCòn Bảng B?"]
    assert gemini["message"] == "Còn Bảng B?", "câu gửi Gemini giữ nguyên"


def test_long_question_is_not_merged_with_history(gemini):
    retriever = FakeRetriever([])
    question = "Cho mình hỏi thời gian thi vòng chung kết của Bảng B là khi nào vậy?"
    RagService(retriever=retriever).answer(question, [{"role": "user", "parts": [{"text": "cũ"}]}])
    assert retriever.queries == [question]


def test_retriever_failure_falls_back_to_faq_keyword(gemini, monkeypatch):
    import faq_matcher

    monkeypatch.setattr(faq_matcher, "find_faq_candidates", lambda cursor, q, limit: [
        {"id": 7, "intent": "Lệ phí", "cau_hoi_mau": "Thi có mất phí không?",
         "tra_loi_chuan": "Miễn phí."},
    ])
    service = RagService(retriever=FakeRetriever(error=RuntimeError("index lỗi")))
    answer = service.answer("lệ phí", cursor=object())
    assert "Trả lời chuẩn: Miễn phí." in gemini["context"]
    assert answer.sources[0].retrieval_method == "faq_keyword"
    assert answer.chunk_ids == []


def test_image_only_message_skips_retrieval(gemini):
    retriever = FakeRetriever([_chunk(1, "abc")])
    RagService(retriever=retriever).answer("", image=ImageInput(b"img", "image/png"))
    assert retriever.queries == []
    assert gemini["context"] is None
    assert gemini["image"] == (b"img", "image/png")


def test_image_with_question_still_uses_knowledge_base(gemini):
    retriever = FakeRetriever([_chunk(1, "Lỗi IndentationError do thụt lề sai.")])
    answer = RagService(retriever=retriever).answer(
        "Lỗi này là gì?", image=ImageInput(b"img", "image/jpeg"))
    assert "IndentationError" in gemini["context"]
    assert answer.chunk_ids == [1]
