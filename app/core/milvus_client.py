"""Milvus 客户端工厂模块

需要 ``[rag]`` extra（``pymilvus``）。缺失时抛
:class:`aiops_core._optional.OptionalDependencyMissing`。
"""

import re

from loguru import logger

try:
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        Function,
        FunctionType,
        MilvusClient,
        MilvusException,
        connections,
        utility,
    )
except ImportError as _exc:  # pragma: no cover - depends on install profile
    from aiops_core._optional import OptionalDependencyMissing

    raise OptionalDependencyMissing(
        "Milvus 检索栈需要 `[rag]` extra。"
        "\n    pip install 'aiops-copilot[rag]'"
    ) from _exc

from app.config import config


class MilvusClientManager:
    """Milvus 客户端管理器"""

    # schema 常量
    ID_MAX_LENGTH: int = 100
    CONTENT_MAX_LENGTH: int = 65535
    DEFAULT_SHARD_NUMBER: int = 2

    def __init__(self) -> None:
        """初始化 Milvus 客户端管理器"""
        self._client: MilvusClient | None = None
        self._collection: Collection | None = None
        self.collection_name = config.milvus_collection_name
        self.vector_dim = config.embedding_dimensions

    def connect(self) -> MilvusClient:
        """
        连接到 Milvus 服务器并初始化 collection

        Returns:
            MilvusClient: Milvus 客户端实例

        Raises:
            RuntimeError: 连接或初始化失败时抛出
        """
        try:
            logger.info(f"正在连接到 Milvus: {config.milvus_host}:{config.milvus_port}")

            # 建立连接
            connections.connect(
                alias="default",
                host=config.milvus_host,
                port=str(config.milvus_port),
                timeout=config.milvus_timeout / 1000,  # 转换为秒
            )

            # 创建客户端
            uri = f"http://{config.milvus_host}:{config.milvus_port}"
            self._client = MilvusClient(uri=uri)
            self._validate_server_version(str(utility.get_server_version()))

            logger.info("成功连接到 Milvus")

            # 检查并创建 collection
            if not self._collection_exists():
                logger.info(f"collection '{self.collection_name}' 不存在，正在创建...")
                self._create_collection()
                logger.info(f"成功创建 collection '{self.collection_name}'")
            else:
                logger.info(f"collection '{self.collection_name}' 已存在")
                self._collection = Collection(self.collection_name)

                # 检查向量维度是否匹配
                schema = self._collection.schema
                field_names = {field.name for field in schema.fields}
                required_fields = {"id", "vector", "content", "sparse_vector", "metadata"}
                missing_fields = required_fields - field_names
                if missing_fields:
                    raise RuntimeError(
                        "Milvus collection schema 不兼容，拒绝原地修改: "
                        f"collection={self.collection_name}, missing={sorted(missing_fields)}。"
                        "请使用新的版本化 collection 名称重建。"
                    )
                vector_field = None
                existing_dim = None
                for field in schema.fields:
                    if field.name == "vector":
                        vector_field = field
                        break

                if (
                    vector_field
                    and hasattr(vector_field, "params")
                    and "dim" in vector_field.params
                ):
                    existing_dim = vector_field.params["dim"]
                    if existing_dim != self.vector_dim:
                        raise RuntimeError(
                            "Milvus collection 向量维度不匹配，拒绝自动删除数据: "
                            f"collection={self.collection_name}, existing={existing_dim}, "
                            f"configured={self.vector_dim}。请使用新的版本化 collection 名称重建索引。"
                        )
                    else:
                        logger.info(f"向量维度匹配: {self.vector_dim}")

            # 加载 collection
            self._load_collection()

            return self._client

        except MilvusException as e:
            logger.error(f"Milvus 操作失败: {e}")
            self.close()
            raise RuntimeError(f"Milvus 操作失败: {e}") from e
        except ConnectionError as e:
            logger.error(f"连接 Milvus 失败: {e}")
            self.close()
            raise RuntimeError(f"连接 Milvus 失败: {e}") from e
        except Exception as e:
            logger.error(f"连接 Milvus 失败: {e}")
            self.close()
            raise RuntimeError(f"连接 Milvus 失败: {e}") from e

    def _collection_exists(self) -> bool:
        """检查 collection 是否存在"""
        # pymilvus 的类型标注可能不准确，实际返回 bool
        result = utility.has_collection(self.collection_name)
        return bool(result)  # type: ignore[arg-type]

    @staticmethod
    def _validate_server_version(version: str) -> None:
        numbers = tuple(int(value) for value in re.findall(r"\d+", version)[:3])
        normalized = numbers + (0,) * (3 - len(numbers))
        if normalized < (2, 6, 0):
            raise RuntimeError(
                f"Milvus Server {version} 不支持 RAG v2 BM25/Jieba 合同；需要 2.6.x"
            )

    def _create_collection(self) -> None:
        """创建 biz collection"""
        # 定义字段
        fields = [
            FieldSchema(
                name="id",
                dtype=DataType.VARCHAR,
                max_length=self.ID_MAX_LENGTH,
                is_primary=True,
            ),
            FieldSchema(
                name="vector",
                dtype=DataType.FLOAT_VECTOR,
                dim=self.vector_dim,
            ),
            FieldSchema(
                name="content",
                dtype=DataType.VARCHAR,
                max_length=self.CONTENT_MAX_LENGTH,
                enable_analyzer=True,
                enable_match=True,
                analyzer_params={
                    "tokenizer": "jieba",
                    "filter": ["removepunct"],
                },
            ),
            FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
            FieldSchema(
                name="metadata",
                dtype=DataType.JSON,
            ),
        ]

        bm25_function = Function(
            name="content_bm25",
            function_type=FunctionType.BM25,
            input_field_names=["content"],
            output_field_names=["sparse_vector"],
        )

        # 创建 schema
        schema = CollectionSchema(
            fields=fields,
            functions=[bm25_function],
            description="AIOps RAG 2.0 dense and BM25 knowledge collection",
            enable_dynamic_field=False,
        )

        # 创建 collection
        self._collection = Collection(
            name=self.collection_name,
            schema=schema,
            num_shards=self.DEFAULT_SHARD_NUMBER,
        )

        # 创建索引
        self._create_index()

    def _create_index(self) -> None:
        """为 vector 字段创建索引"""
        if self._collection is None:
            raise RuntimeError("Collection 未初始化")

        dense_index_params = {
            "metric_type": config.milvus_metric_type,
            "index_type": "HNSW",
            "params": {"M": 32, "efConstruction": 200},
        }

        _ = self._collection.create_index(
            field_name="vector",
            index_params=dense_index_params,
        )
        _ = self._collection.create_index(
            field_name="sparse_vector",
            index_params={
                "metric_type": "BM25",
                "index_type": "SPARSE_INVERTED_INDEX",
                "params": {"inverted_index_algo": "DAAT_MAXSCORE"},
            },
        )

        logger.info("成功创建 dense HNSW 与 sparse BM25 索引")

    def publish_alias(self, collection_name: str | None = None) -> str:
        """Atomically point the stable read alias at a validated collection."""
        target = collection_name or self.collection_name
        alias = config.milvus_collection_alias
        if not utility.has_collection(target):
            raise ValueError(f"collection 不存在: {target}")
        alias_owner = next(
            (
                existing
                for existing in utility.list_collections()
                if alias in set(utility.list_aliases(existing))
            ),
            None,
        )
        if alias_owner == target:
            return alias
        if alias_owner is None:
            utility.create_alias(target, alias)
        else:
            utility.alter_alias(target, alias)
        logger.info("Milvus alias '{}' now points to '{}'", alias, target)
        return alias

    def rollback_alias(self, collection_name: str) -> str:
        """Move the stable alias back to an explicitly named existing collection."""
        if not utility.has_collection(collection_name):
            raise ValueError(f"collection 不存在: {collection_name}")
        utility.alter_alias(collection_name, config.milvus_collection_alias)
        return config.milvus_collection_alias

    def _load_collection(self) -> None:
        """加载 collection 到内存"""
        if self._collection is None:
            self._collection = Collection(self.collection_name)

        # 检查 collection 是否已加载（兼容多版本）
        try:
            # 方法 1: 尝试使用 utility.load_state（新版本）
            load_state = utility.load_state(self.collection_name)
            # load_state 返回字符串或枚举，如 "Loaded" 或 "NotLoad"
            state_name = getattr(load_state, "name", str(load_state))
            if state_name != "Loaded":
                self._collection.load()
                logger.info(f"成功加载 collection '{self.collection_name}'")
            else:
                logger.info(f"Collection '{self.collection_name}' 已加载")
        except AttributeError:
            # 方法 2: 直接尝试加载，捕获 "already loaded" 异常
            try:
                self._collection.load()
                logger.info(f"成功加载 collection '{self.collection_name}'")
            except MilvusException as e:
                error_msg = str(e).lower()
                if "already loaded" in error_msg or "loaded" in error_msg:
                    logger.info(f"Collection '{self.collection_name}' 已加载")
                else:
                    raise
        except Exception as e:
            logger.error(f"加载 collection 失败: {e}")
            raise

    def get_collection(self) -> Collection:
        """
        获取 collection 实例

        Returns:
            Collection: collection 实例

        Raises:
            RuntimeError: collection 未初始化时抛出
        """
        if self._collection is None:
            raise RuntimeError("Collection 未初始化，请先调用 connect()")
        return self._collection

    def get_read_collection(self) -> Collection:
        """Resolve the stable alias on every request so rollback is immediate."""
        alias = config.milvus_collection_alias
        alias_exists = any(
            alias in set(utility.list_aliases(collection_name))
            for collection_name in utility.list_collections()
        )
        return Collection(alias) if alias_exists else self.get_collection()

    def health_check(self) -> bool:
        """
        健康检查

        Returns:
            bool: True 表示健康，False 表示异常
        """
        try:
            if self._client is None:
                return False

            # 真实 RPC 验证服务端可达，而非仅检查本地连接注册表
            _ = utility.get_server_version()
            return True

        except (MilvusException, ConnectionError) as e:
            logger.error(f"Milvus 健康检查失败: {e}")
            return False
        except Exception as e:
            logger.error(f"Milvus 健康检查失败: {e}")
            return False

    def close(self) -> None:
        """关闭连接"""
        errors = []

        try:
            if self._collection is not None:
                self._collection.release()
                self._collection = None
        except Exception as e:
            errors.append(f"释放 collection 失败: {e}")

        try:
            if connections.has_connection("default"):
                connections.disconnect("default")
        except Exception as e:
            errors.append(f"断开连接失败: {e}")

        self._client = None

        if errors:
            error_msg = "; ".join(errors)
            logger.error(f"关闭 Milvus 连接时出现错误: {error_msg}")
        else:
            logger.info("已关闭 Milvus 连接")

    def __enter__(self) -> "MilvusClientManager":
        """上下文管理器入口"""
        _ = self.connect()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        """上下文管理器退出"""
        self.close()


# 全局单例
milvus_manager = MilvusClientManager()
