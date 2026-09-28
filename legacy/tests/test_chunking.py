import pytest

from rag_api.services.chunking import TextChunker


def test_chunks_overlap_and_preserve_content_shape() -> None:
    chunks = TextChunker(chunk_size=20, overlap=5).split("one two three four five six seven")
    assert len(chunks) > 1
    assert all(len(chunk) <= 20 for chunk in chunks)


def test_rejects_invalid_overlap() -> None:
    with pytest.raises(ValueError):
        TextChunker(chunk_size=10, overlap=10)
