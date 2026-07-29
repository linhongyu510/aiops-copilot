from langchain_core.documents import Document

from app.tools.knowledge_tool import diversify_by_source


def test_diversify_by_source_prefers_distinct_documents() -> None:
    docs = [
        Document(page_content="a1", metadata={"_file_name": "a.md"}),
        Document(page_content="a2", metadata={"_file_name": "a.md"}),
        Document(page_content="b1", metadata={"_file_name": "b.md"}),
        Document(page_content="c1", metadata={"_file_name": "c.md"}),
    ]
    selected = diversify_by_source(docs, 3)
    assert [doc.page_content for doc in selected] == ["a1", "b1", "c1"]
