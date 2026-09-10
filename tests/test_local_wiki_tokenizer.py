"""local_wiki 确定性中文分词器单元测试。"""

from app.services.local_wiki.tokenizer import tokenize, tokenize_for_fts


def test_ascii_words_lowercased() -> None:
    assert tokenize("Hello World 123") == ["hello", "world", "123"]


def test_chinese_bigrams_and_unigrams() -> None:
    assert tokenize("使用") == ["使用", "使", "用"]


def test_mixed_chinese_ascii() -> None:
    tokens = tokenize("CPU使用率")
    assert "cpu" in tokens
    # 连续中文段产出相邻 bigram。
    assert "使用" in tokens


def test_punctuation_skipped() -> None:
    tokens = tokenize("你好，世界！")
    assert "你好" in tokens
    assert "世界" in tokens
    # 标点与空白不应成为 token。
    assert "，" not in tokens
    assert "！" not in tokens
    assert " " not in tokens


def test_empty_string() -> None:
    assert tokenize("") == []


def test_tokenize_for_fts_returns_space_separated() -> None:
    out = tokenize_for_fts("CPU 使用率")
    assert isinstance(out, str)
    assert out.split() == tokenize("CPU 使用率")


def test_deterministic() -> None:
    text = "CPU使用率过高 排查步骤 CPU"
    assert tokenize(text) == tokenize(text)
    assert tokenize_for_fts(text) == tokenize_for_fts(text)


def test_cpu_high_usage_query_tokens() -> None:
    tokens = tokenize("CPU使用率过高")
    # 与 aiops-docs/cpu_high_usage.md 标题重叠的 token。
    assert "cpu" in tokens
    assert "使用" in tokens
    assert "过高" in tokens
