"""向量索引服务模块

薄委托层：公开接口（``index_single_file`` / ``index_directory`` /
``IndexingResult`` / ``resolve_index_directory`` / ``upload_path``）保持不变，
实际的切分、向量化与写入由可插拔检索后端（local_wiki 或 milvus）完成。
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.services.retrieval_backend import get_retrieval_backend


class IndexingResult:
    """索引结果类"""

    def __init__(self):
        self.success = False
        self.directory_path = ""
        self.total_files = 0
        self.success_count = 0
        self.fail_count = 0
        self.start_time: datetime | None = None
        self.end_time: datetime | None = None
        self.error_message = ""
        self.failed_files: dict[str, str] = {}

    def increment_success_count(self):
        """增加成功计数"""
        self.success_count += 1

    def increment_fail_count(self):
        """增加失败计数"""
        self.fail_count += 1

    def add_failed_file(self, file_path: str, error: str):
        """添加失败文件"""
        self.failed_files[file_path] = error

    def get_duration_ms(self) -> int:
        """获取耗时（毫秒）"""
        if self.start_time and self.end_time:
            return int((self.end_time - self.start_time).total_seconds() * 1000)
        return 0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "directory_path": self.directory_path,
            "total_files": self.total_files,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "duration_ms": self.get_duration_ms(),
            "error_message": self.error_message,
            "failed_files": self.failed_files,
        }


class VectorIndexService:
    """向量索引服务 - 委托给可插拔检索后端"""

    def __init__(self):
        """初始化向量索引服务"""
        self.upload_path = "./uploads"
        self.backend = get_retrieval_backend()
        logger.info("向量索引服务初始化完成")

    def resolve_index_directory(self, directory_path: str | None = None) -> Path:
        """解析待索引目录，并限制在 uploads 根目录内。"""
        upload_root = Path(self.upload_path).resolve()
        target = Path(directory_path).resolve() if directory_path else upload_root
        try:
            target.relative_to(upload_root)
        except ValueError as exc:
            raise ValueError(f"仅允许索引上传目录及其子目录: {upload_root}") from exc
        return target

    def index_directory(self, directory_path: str | None = None) -> IndexingResult:
        """
        索引指定目录下的所有文件

        Args:
            directory_path: 目录路径（可选，默认使用配置的上传目录）

        Returns:
            IndexingResult: 索引结果
        """
        return self.backend.index_directory(directory_path)

    def index_single_file(self, file_path: str):
        """
        索引单个文件（委托给检索后端）

        Args:
            file_path: 文件路径

        Raises:
            ValueError: 文件不存在时抛出
            RuntimeError: 索引失败时抛出
        """
        self.backend.index_file(file_path)


# 全局单例
vector_index_service = VectorIndexService()
