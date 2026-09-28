import pytest

from knowledge.document_loader import DocumentSection
from knowledge.text_chunker import chunk_sections, split_text


def test_short_text_is_single_chunk():
    assert split_text("Lệ phí thi Bảng A là 0 đồng.", 100, 10) == ["Lệ phí thi Bảng A là 0 đồng."]


def test_empty_text_returns_no_chunk():
    assert split_text("   \n  ", 100, 10) == []


def test_chunks_respect_size_and_keep_all_content():
    sentences = [f"Câu số {i} nói về thể lệ cuộc thi Python Master." for i in range(60)]
    text = " ".join(sentences)
    chunks = split_text(text, 300, 50)

    assert len(chunks) > 1
    assert all(len(chunk) <= 300 for chunk in chunks)
    for sentence in sentences:
        assert any(sentence in chunk for chunk in chunks), sentence


def test_consecutive_chunks_overlap():
    text = " ".join(f"Điều {i}: thí sinh phải tuân thủ quy định số {i}." for i in range(40))
    chunks = split_text(text, 250, 80)
    for previous, current in zip(chunks, chunks[1:]):
        head = current[:30]
        assert head in previous, "chunk sau phải bắt đầu bằng phần đuôi của chunk trước"


def test_prefers_paragraph_boundaries():
    para_a = "Bảng A dành cho học sinh 12-18 tuổi. " * 5
    para_b = "Bảng B dành cho sinh viên 19-24 tuổi. " * 5
    chunks = split_text(f"{para_a.strip()}\n\n{para_b.strip()}", 220, 0)
    assert chunks[0].startswith("Bảng A") and "Bảng B" not in chunks[0]
    assert chunks[-1].startswith("Bảng B")


def test_unbreakable_text_is_hard_cut():
    chunks = split_text("x" * 250, 100, 0)
    assert [len(c) for c in chunks] == [100, 100, 50]


@pytest.mark.parametrize("size,overlap", [(0, 0), (100, 100), (100, -1)])
def test_invalid_parameters(size, overlap):
    with pytest.raises(ValueError):
        split_text("abc", size, overlap)


def test_chunk_sections_keeps_page_and_metadata():
    sections = [
        DocumentSection(text="Trang một. " * 30, page_number=1),
        DocumentSection(text="Dòng CSV", metadata={"row_number": 2}),
    ]
    chunks = chunk_sections(sections, 120, 20)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].page_number == 1
    assert chunks[-1].metadata == {"row_number": 2}
    assert len(chunks[0].content_hash) == 64
    assert chunks[0].token_estimate > 0
