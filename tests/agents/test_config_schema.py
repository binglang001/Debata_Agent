"""测试 agents 相关配置默认值。"""

# ============================================================
# Schema 新字段
# ============================================================


def test_schema_long_term_memory_config_defaults():
    from app_config import LongTermMemoryConfig

    c = LongTermMemoryConfig()
    assert c.mode == "file"
    assert not hasattr(c, "keyword_trigger_save")
    assert c.rag_top_k == 5


def test_schema_embedding_feature_default_disabled():
    from app_config import EmbeddingFeatureConfig

    c = EmbeddingFeatureConfig()
    assert c.enabled is False
    assert c.type == "api"
    assert c.local_quality == "performance"


def test_schema_refocus_interval_default():
    from app_config import AgentConfig

    c = AgentConfig(provider="x", model="y")
    assert c.refocus_interval == 5
