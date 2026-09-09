"""知识检索工具 - 从向量数据库中检索相关信息"""

import time

from langchain_core.documents import Document
from langchain_core.tools import tool
from loguru import logger

from app.config import config
from app.observability import tool_metrics
from app.services.hybrid_retrieval_service import hybrid_retrieval_service


def diversify_by_source(docs: list[Document], limit: int) -> list[Document]:
    """Keep the highest-ranked chunk per source before filling remaining slots."""
    selected: list[Document] = []
    deferred: list[Document] = []
    seen_sources: set[str] = set()
    for doc in docs:
        source = str(
            doc.metadata.get("_file_name")
            or doc.metadata.get("source")
            or doc.metadata.get("_source")
            or ""
        )
        if source and source not in seen_sources:
            selected.append(doc)
            seen_sources.add(source)
        else:
            deferred.append(doc)
        if len(selected) >= limit:
            return selected
    return (selected + deferred)[:limit]


@tool(response_format="content_and_artifact")
async def retrieve_knowledge(query: str) -> tuple[str, list[Document]]:
    """从知识库中检索相关信息来回答问题

    当用户的问题涉及专业知识、文档内容或需要参考资料时，使用此工具。

    Args:
        query: 用户的问题或查询

    Returns:
        Tuple[str, List[Document]]: (格式化的上下文文本, 原始文档列表)
    """
    started = time.perf_counter()
    try:
        logger.info(f"知识检索工具被调用: query='{query}'")

        candidates, trace = await hybrid_retrieval_service.search(query, config.rag_top_k)
        docs = [
            Document(
                page_content=candidate.content,
                metadata={
                    **candidate.metadata,
                    "_retrieval": {
                        "rrf_score": candidate.rrf_score,
                        "reranker_score": candidate.reranker_score,
                        "branches": candidate.branch_ranks,
                        "final_rank": candidate.final_rank,
                        "degradations": trace.degradations,
                    },
                },
            )
            for candidate in candidates
        ]

        if not docs:
            logger.warning("未检索到相关文档")
            tool_metrics.record(
                "retrieve_knowledge",
                success=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                arguments={"query": query},
            )
            return "没有找到相关信息。", []

        # 格式化文档为上下文
        context = format_docs(docs)

        logger.info(f"检索到 {len(docs)} 个相关文档")
        tool_metrics.record(
            "retrieve_knowledge",
            success=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            arguments={"query": query},
        )
        return context, docs

    except Exception as e:
        logger.error(f"知识检索工具调用失败: {e}")
        tool_metrics.record(
            "retrieve_knowledge",
            success=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            arguments={"query": query},
        )
        return (
            f"检索知识时发生错误: {str(e)}。知识库当前不可用，不要据此编造答案。",
            [],
        )


def format_docs(docs: list[Document]) -> str:
    """
    格式化文档列表为上下文文本

    Args:
        docs: 文档列表

    Returns:
        str: 格式化的上下文文本
    """
    formatted_parts = []

    for i, doc in enumerate(docs, 1):
        # 提取元数据
        metadata = doc.metadata
        source = metadata.get("_file_name", "未知来源")

        # 提取标题信息 (如果有)
        headers = []
        for key in ["h1", "h2", "h3"]:
            if key in metadata and metadata[key]:
                headers.append(metadata[key])

        header_str = " > ".join(headers) if headers else ""

        # 构建格式化文本
        formatted = f"[{i}]"
        if header_str:
            formatted += f"\n标题: {header_str}"
        formatted += f"\n来源: {source}"
        formatted += f"\n内容:\n{doc.page_content}\n"

        formatted_parts.append(formatted)

    return "\n".join(formatted_parts)
