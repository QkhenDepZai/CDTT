"""
Knowledge Base cho RAG (Giai đoạn 1).

Luồng nạp tri thức:
    file (PDF/TXT/CSV/DOCX/MD) hoặc bảng faqs
      -> document_loader  : trích xuất text, chuẩn hoá Unicode tiếng Việt
      -> text_chunker     : cắt đoạn có overlap
      -> embedding_service: gọi Gemini Embedding (batch + retry)
      -> repository       : lưu knowledge_metadata / knowledge_chunks (MySQL)
      -> vector_index     : chỉ mục cosine trong RAM (numpy) để truy vấn nhanh

Luồng truy vấn: retriever.KnowledgeRetriever.search(câu hỏi) -> top-K chunk.
"""
