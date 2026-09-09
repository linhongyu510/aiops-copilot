import os

import pytest
from pymilvus import Collection, connections, utility


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("AIOPS_RUN_MILVUS_INTEGRATION") != "1",
    reason="set AIOPS_RUN_MILVUS_INTEGRATION=1 with vector-database.yml running",
)
def test_milvus_container_collection_matches_runtime_contract() -> None:
    alias = "container-integration"
    collection_name = os.getenv(
        "AIOPS_MILVUS_COLLECTION", "aiops_kb_bge_large_zh_v1_5_v1"
    )
    connections.connect(
        alias=alias,
        host=os.getenv("AIOPS_MILVUS_HOST", "127.0.0.1"),
        port=os.getenv("AIOPS_MILVUS_PORT", "19530"),
    )
    try:
        assert collection_name in utility.list_collections(using=alias)
        collection = Collection(collection_name, using=alias)
        vector_field = next(
            field for field in collection.schema.fields if field.name == "vector"
        )
        assert vector_field.params["dim"] == 1024
        assert any(field.name == "sparse_vector" for field in collection.schema.fields)
        assert collection.num_entities > 0
    finally:
        connections.disconnect(alias)
