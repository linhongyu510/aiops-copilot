"""Milvus 健康检查：通过 utility.get_server_version 真实 RPC 验证服务端可达"""

import json
from unittest.mock import Mock

import pytest
from pymilvus import MilvusException

import app.core.milvus_client as milvus_module
from app.config import config
from app.core.milvus_client import MilvusClientManager


def test_health_check_true_when_server_version_ok(monkeypatch) -> None:
    manager = MilvusClientManager()
    manager._client = Mock()
    monkeypatch.setattr(
        milvus_module.utility, "get_server_version", Mock(return_value="2.4.3")
    )

    assert manager.health_check() is True


def test_health_check_false_when_rpc_fails(monkeypatch) -> None:
    manager = MilvusClientManager()
    manager._client = Mock()
    monkeypatch.setattr(
        milvus_module.utility,
        "get_server_version",
        Mock(side_effect=MilvusException(message="server unreachable")),
    )

    assert manager.health_check() is False


def test_health_check_false_without_client() -> None:
    assert MilvusClientManager().health_check() is False


def test_server_version_contract_requires_milvus_2_6() -> None:
    MilvusClientManager._validate_server_version("2.6.9")
    with pytest.raises(RuntimeError, match="需要 2.6.x"):
        MilvusClientManager._validate_server_version("2.5.10")


def test_v2_schema_contains_dense_jieba_bm25_and_1024_dimensions(monkeypatch) -> None:
    collection = Mock()
    collection_factory = Mock(return_value=collection)
    monkeypatch.setattr(milvus_module, "Collection", collection_factory)
    manager = MilvusClientManager()
    manager.vector_dim = 1024
    manager._create_collection()

    schema = collection_factory.call_args.kwargs["schema"]
    fields = {field.name: field for field in schema.fields}
    assert fields["vector"].params["dim"] == 1024
    analyzer = json.loads(fields["content"].params["analyzer_params"])
    assert analyzer["tokenizer"] == "jieba"
    assert fields["sparse_vector"].dtype.name == "SPARSE_FLOAT_VECTOR"
    assert schema.functions[0].input_field_names == ["content"]
    assert schema.functions[0].output_field_names == ["sparse_vector"]
    assert collection.create_index.call_count == 2


def test_publish_alias_atomically_moves_existing_alias(monkeypatch) -> None:
    manager = MilvusClientManager()
    target = manager.collection_name
    monkeypatch.setattr(milvus_module.utility, "has_collection", Mock(return_value=True))
    monkeypatch.setattr(
        milvus_module.utility,
        "list_collections",
        Mock(return_value=["legacy_collection", target]),
    )
    monkeypatch.setattr(
        milvus_module.utility,
        "list_aliases",
        Mock(
            side_effect=lambda name: (
                [config.milvus_collection_alias] if name == "legacy_collection" else []
            )
        ),
    )
    create_alias = Mock()
    alter_alias = Mock()
    monkeypatch.setattr(milvus_module.utility, "create_alias", create_alias)
    monkeypatch.setattr(milvus_module.utility, "alter_alias", alter_alias)

    assert manager.publish_alias() == config.milvus_collection_alias
    create_alias.assert_not_called()
    alter_alias.assert_called_once_with(target, config.milvus_collection_alias)
