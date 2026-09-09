"""Token-aware document splitting with stable, auditable chunk metadata."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from loguru import logger

from app.config import config


class DocumentSplitterService:
    """文档分割服务 - 使用 LangChain 的分割器"""

    def __init__(self):
        """初始化文档分割服务"""
        self.chunk_size = config.chunk_max_size
        self.chunk_overlap = config.chunk_overlap

        # Markdown 标题分割器 (只按一级和二级标题分割，减少分片数)
        self.markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("#", "h1"),
                ("##", "h2"),
                # 不再按三级标题分割，避免过度碎片化
            ],
            strip_headers=False,  # 保留标题在内容中
        )

        self._tokenizer: Any | None = None
        self._tokenizer_checked = False

        # The configured size is expressed in model tokens.  If the BGE tokenizer is
        # not cached yet, the deterministic fallback still keeps Chinese chunks close
        # to the 512-token model limit without triggering a download at import time.
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=self._token_length,
            separators=["\n\n", "\n", "。", "；", "，", " ", ""],
            is_separator_regex=False,
        )

        logger.info(
            f"文档分割服务初始化完成, chunk_size={self.chunk_size}, "
            f"overlap={self.chunk_overlap}"
        )

    def split_markdown(self, content: str, file_path: str = "") -> list[Document]:
        """
        分割 Markdown 文档 (两阶段分割 + 合并小片段)

        Args:
            content: Markdown 内容
            file_path: 文件路径 (用于元数据)

        Returns:
            List[Document]: 文档分片列表
        """
        if not content or not content.strip():
            logger.warning(f"Markdown 文档内容为空: {file_path}")
            return []

        try:
            # 第一阶段: 按标题分割
            md_docs = self.markdown_splitter.split_text(content)

            # 第二阶段: 按大小进一步分割
            docs_after_split = self.text_splitter.split_documents(md_docs)

            # 第三阶段: 合并过小的分片，同时保持在模型 token 上限内。
            final_docs = self._merge_small_chunks(docs_after_split, min_size=80)

            final_docs = self._finalize_documents(final_docs, file_path)
            logger.info(f"Markdown 分割完成: {file_path} -> {len(final_docs)} 个分片")
            return final_docs

        except Exception as e:
            logger.error(f"Markdown 分割失败: {file_path}, 错误: {e}")
            raise

    def split_text(self, content: str, file_path: str = "") -> list[Document]:
        """
        分割普通文本文档

        Args:
            content: 文本内容
            file_path: 文件路径 (用于元数据)

        Returns:
            List[Document]: 文档分片列表
        """
        if not content or not content.strip():
            logger.warning(f"文本文档内容为空: {file_path}")
            return []

        try:
            # 直接使用递归字符分割器
            docs = self.text_splitter.create_documents(texts=[content])
            docs = self._finalize_documents(docs, file_path)

            logger.info(f"文本分割完成: {file_path} -> {len(docs)} 个分片")
            return docs

        except Exception as e:
            logger.error(f"文本分割失败: {file_path}, 错误: {e}")
            raise

    def split_document(self, content: str, file_path: str = "") -> list[Document]:
        """
        智能分割文档 (根据文件类型选择分割器)

        Args:
            content: 文档内容
            file_path: 文件路径

        Returns:
            List[Document]: 文档分片列表
        """
        if file_path.endswith(".md") and config.rag_splitter_strategy == "markdown-header":
            return self.split_markdown(content, file_path)
        return self.split_text(content, file_path)

    def _merge_small_chunks(self, documents: list[Document], min_size: int = 300) -> list[Document]:
        """
        合并太小的分片

        Args:
            documents: 文档列表
            min_size: 最小分片大小 (字符数)

        Returns:
            List[Document]: 合并后的文档列表
        """
        if not documents:
            return []

        merged_docs = []
        current_doc = None

        for doc in documents:
            doc_size = self._token_length(doc.page_content)

            if current_doc is None:
                # 第一个文档
                current_doc = doc
            elif (
                doc_size < min_size
                and self._token_length(current_doc.page_content) + doc_size <= self.chunk_size
            ):
                # 当前文档太小且合并后不超过配置上限，则合并。
                current_doc.page_content += "\n\n" + doc.page_content
                # 保留主文档的元数据
            else:
                # 保存当前文档，开始新文档
                merged_docs.append(current_doc)
                current_doc = doc

        # 添加最后一个文档
        if current_doc is not None:
            merged_docs.append(current_doc)

        return merged_docs

    def _token_length(self, text: str) -> int:
        """Return BGE token length without downloading a model during app import."""
        if not self._tokenizer_checked:
            self._tokenizer_checked = True
            try:
                from transformers import AutoTokenizer

                self._tokenizer = AutoTokenizer.from_pretrained(
                    config.local_embedding_model,
                    cache_dir=config.local_embedding_cache_dir or None,
                    revision=config.local_embedding_revision,
                    local_files_only=True,
                    trust_remote_code=False,
                )
            except Exception:
                self._tokenizer = None
        if self._tokenizer is not None:
            token_count = len(self._tokenizer.encode(text, add_special_tokens=False))
            # Keep the historical hard character bound as an additional safety
            # invariant while enforcing the model-token bound.
            return max(token_count, len(text))
        chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
        latin = len(re.findall(r"[A-Za-z0-9_./:-]+", text))
        punctuation = len(re.findall(r"[^\w\s\u4e00-\u9fff]", text))
        return max(chinese + latin + punctuation, len(text))

    @staticmethod
    def _normalized_text(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _finalize_documents(
        self,
        documents: list[Document],
        file_path: str,
    ) -> list[Document]:
        path = Path(file_path)
        source = path.as_posix()
        doc_id = path.stem.lower().replace(" ", "_")
        build_time = datetime.now(UTC).isoformat()
        manifest_metadata: dict[str, Any] = {}
        manifest_path = path.parent / "CORPUS_MANIFEST.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_metadata = next(
                    (
                        item
                        for item in manifest.get("documents", [])
                        if item.get("file") == path.name
                    ),
                    {},
                )
            except (OSError, json.JSONDecodeError):
                manifest_metadata = {}
        finalized: list[Document] = []
        seen_hashes: set[str] = set()
        for ordinal, doc in enumerate(documents):
            normalized = self._normalized_text(doc.page_content)
            content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if not normalized or content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)
            headers = [
                str(doc.metadata.get(key, "")).strip()
                for key in ("h1", "h2", "h3")
                if doc.metadata.get(key)
            ]
            title_path = " > ".join(headers)
            stable_seed = f"{doc_id}\n{title_path}\n{normalized}"
            chunk_id = hashlib.sha256(stable_seed.encode("utf-8")).hexdigest()[:32]
            doc.metadata.update(
                {
                    "_source": source,
                    "_extension": path.suffix.lower(),
                    "_file_name": path.name,
                    "doc_id": doc_id,
                    "chunk_id": chunk_id,
                    "chunk_index": ordinal,
                    "content_hash": content_hash,
                    "title_path": title_path,
                    "corpus_version": config.corpus_version,
                    "document_version": manifest_metadata.get("version", "1.0"),
                    "category": manifest_metadata.get("category", "unclassified"),
                    "license": manifest_metadata.get("license", "project-original"),
                    "review_status": manifest_metadata.get("review_status", "pending"),
                    "source_urls": manifest_metadata.get("source_urls", []),
                    "embedding_model": config.local_embedding_model,
                    "embedding_revision": config.local_embedding_revision,
                    "chunk_size": config.chunk_max_size,
                    "chunk_overlap": config.chunk_overlap,
                    "index_build_time": build_time,
                    "token_count": self._token_length(doc.page_content),
                }
            )
            finalized.append(doc)
        return finalized


# 全局单例
document_splitter_service = DocumentSplitterService()
