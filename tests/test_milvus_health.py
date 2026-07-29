"""Milvus 健康检查：通过 utility.get_server_version 真实 RPC 验证服务端可达"""

from unittest.mock import Mock

from pymilvus import MilvusException

import app.core.milvus_client as milvus_module
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
