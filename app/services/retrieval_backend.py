"""检索后端抽象层：在 local_wiki 与 milvus 之间可插拔。

``hybrid_retrieval_service`` 与 ``vector_index_service`` 不再直接依赖具体向量库，
而是通过 :func:`get_retrieval_backend` 获取一个后端单例。后端实现统一提供：

- ``initialize()`` / ``close()`` 生命周期；
- ``dense_search(vector, top_k, branch)`` / ``bm25_search(query, top_k, branch)``；
- ``index_file(file_path)`` / ``index_directory(directory_path=None)``；
- ``health()`` 与 ``backend_type`` / ``supports_dense`` / ``supports_bm25`` 能力位。

默认后端为 ``local_wiki``（零第三方依赖的 SQLite FTS5/BM25），设置
``AIOPS_RETRIEVAL_BACKEND=milvus`` 可切换到原 Milvus 稠密 + BM25 栈。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.models.retrieval import RetrievalCandidate
from app.services.document_splitter_service import document_splitter_service
from app.services.vector_store_manager import vector_store_manager


class MilvusBackend:
    """封装现有 Milvus 稠密 + BM25 检索与索引逻辑。

    所有方法均为同步阻塞调用，由调用方通过 ``asyncio.to_thread`` 调度。
    """

    backend_type: str = "milvus"
    supports_dense: bool = True
    supports_bm25: bool = True

    def __init__(self) -> None:
        self.upload_path = "./uploads"

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def initialize(self) -> None:
        """连接 Milvus 并加载 collection。"""
        milvus_manager.connect()

    def close(self) -> None:
        """关闭 Milvus 连接。"""
        milvus_manager.close()

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    @staticmethod
    def _entity_value(hit: Any, field: str, default: Any = None) -> Any:
        """兼容 pymilvus Hit 对象与 dict 形式的命中结果。"""
        entity = getattr(hit, "entity", None)
        if entity is not None:
            try:
                return entity.get(field)
            except Exception:
                pass
        if isinstance(hit, dict):
            return hit.get("entity", {}).get(field, hit.get(field, default))
        return default

    @classmethod
    def _candidate_from_hit(
        cls,
        hit: Any,
        branch: str,
        rank: int,
    ) -> RetrievalCandidate:
        metadata = dict(cls._entity_value(hit, "metadata", {}) or {})
        metadata["backend"] = "milvus"
        chunk_id = str(
            metadata.get("chunk_id") or cls._entity_value(hit, "id") or getattr(hit, "id", "")
        )
        distance = (
            hit.get("distance", hit.get("score", 0.0))
            if isinstance(hit, dict)
            else getattr(hit, "distance", getattr(hit, "score", 0.0))
        )
        return RetrievalCandidate(
            chunk_id=chunk_id,
            content=str(cls._entity_value(hit, "content", "") or ""),
            metadata=metadata,
            branch_ranks={branch: rank},
            raw_scores={branch: float(distance or 0.0)},
        )

    def dense_search(
        self,
        vector: list[float],
        top_k: int,
        branch: str,
    ) -> list[RetrievalCandidate]:
        collection = milvus_manager.get_read_collection()
        result = collection.search(
            data=[vector],
            anns_field="vector",
            param={
                "metric_type": config.milvus_metric_type,
                "params": {"ef": 128},
            },
            limit=top_k,
            output_fields=["id", "content", "metadata"],
        )
        hits = result[0] if result else []
        return [
            self._candidate_from_hit(hit, branch, rank) for rank, hit in enumerate(hits, start=1)
        ]

    def bm25_search(
        self,
        query: str,
        top_k: int,
        branch: str,
    ) -> list[RetrievalCandidate]:
        collection = milvus_manager.get_read_collection()
        result = collection.search(
            data=[query],
            anns_field="sparse_vector",
            param={"metric_type": "BM25", "params": {}},
            limit=top_k,
            output_fields=["id", "content", "metadata"],
        )
        hits = result[0] if result else []
        return [
            self._candidate_from_hit(hit, branch, rank) for rank, hit in enumerate(hits, start=1)
        ]

    # ------------------------------------------------------------------ #
    # 索引
    # ------------------------------------------------------------------ #
    def index_file(self, file_path: str) -> None:
        """读取文件、切分、原子 upsert 后清理同来源旧 chunk。"""
        path = Path(file_path).resolve()

        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {file_path}")

        logger.info(f"开始索引文件: {path}")

        try:
            # 1. 读取文件内容
            content = path.read_text(encoding="utf-8")
            logger.info(f"读取文件: {path}, 内容长度: {len(content)} 字符")

            # 2. 先构建稳定 chunk，再原子 upsert；失败时保留原索引证据。
            normalized_path = path.as_posix()
            documents = document_splitter_service.split_document(content, normalized_path)
            logger.info(f"文档分割完成: {file_path} -> {len(documents)} 个分片")

            # 3. Upsert 成功后再清理同来源不再存在的旧 chunk。
            if documents:
                vector_store_manager.add_documents(documents)
                vector_store_manager.delete_stale_by_source(
                    normalized_path,
                    {str(document.metadata["chunk_id"]) for document in documents},
                )
                logger.info(f"文件索引完成: {file_path}, 共 {len(documents)} 个分片")
            else:
                logger.warning(f"文件内容为空或无法分割: {file_path}")

        except Exception as e:
            logger.error(f"索引文件失败: {file_path}, 错误: {e}")
            raise RuntimeError(f"索引文件失败: {e}") from e

    def index_directory(self, directory_path: str | None = None) -> Any:
        """索引目录下的 .txt/.md 文件，成功后发布读别名。"""
        from app.services.vector_index_service import IndexingResult

        result = IndexingResult()
        result.start_time = datetime.now()

        try:
            # 使用指定目录或默认上传目录
            target_path = directory_path if directory_path else self.upload_path
            upload_root = Path(self.upload_path).resolve()
            target = Path(directory_path).resolve() if directory_path else upload_root
            try:
                target.relative_to(upload_root)
            except ValueError as exc:
                raise ValueError(f"仅允许索引上传目录及其子目录: {upload_root}") from exc
            dir_path = target

            if not dir_path.exists() or not dir_path.is_dir():
                raise ValueError(f"目录不存在或不是有效目录: {target_path}")

            result.directory_path = str(dir_path)

            # 获取所有支持的文件
            files = list(dir_path.glob("*.txt")) + list(dir_path.glob("*.md"))

            if not files:
                logger.warning(f"目录中没有找到支持的文件: {target_path}")
                result.total_files = 0
                result.success = True
                result.end_time = datetime.now()
                return result

            result.total_files = len(files)
            logger.info(f"开始索引目录: {target_path}, 找到 {len(files)} 个文件")

            # 遍历并索引每个文件
            for file_path in files:
                try:
                    self.index_file(str(file_path))
                    result.increment_success_count()
                    logger.info(f"✓ 文件索引成功: {file_path.name}")
                except Exception as e:
                    result.increment_fail_count()
                    result.add_failed_file(str(file_path), str(e))
                    logger.error(f"✗ 文件索引失败: {file_path.name}, 错误: {e}")

            result.success = result.fail_count == 0
            if result.success and result.success_count:
                milvus_manager.publish_alias()
            result.end_time = datetime.now()

            logger.info(
                f"目录索引完成: 总数={result.total_files}, "
                f"成功={result.success_count}, 失败={result.fail_count}"
            )

            return result

        except Exception as e:
            logger.error(f"索引目录失败: {e}")
            result.success = False
            result.error_message = str(e)
            result.end_time = datetime.now()
            return result

    # ------------------------------------------------------------------ #
    # 健康
    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        try:
            ok = milvus_manager.health_check()
        except Exception as e:
            logger.warning(f"Milvus 健康检查失败: {e}")
            return {"status": "error", "message": f"Milvus 检查失败: {str(e)}"}
        status = "connected" if ok else "disconnected"
        message = "Milvus 连接正常" if ok else "Milvus 连接异常"
        return {"status": status, "message": message}


# ---------------------------------------------------------------------- #
# 后端单例工厂
# ---------------------------------------------------------------------- #
_backend_instance: Any = None


def get_retrieval_backend() -> Any:
    """根据 ``config.retrieval_backend`` 返回后端单例。"""
    global _backend_instance
    if _backend_instance is None:
        if config.retrieval_backend == "milvus":
            _backend_instance = MilvusBackend()
        else:
            from app.services.local_wiki import LocalWikiBackend

            _backend_instance = LocalWikiBackend()
    return _backend_instance


def reset_retrieval_backend() -> None:
    """测试用：重置单例缓存。"""
    global _backend_instance
    _backend_instance = None
