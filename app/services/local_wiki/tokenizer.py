"""确定性中文分词器。

仅依赖 Python 标准库，不使用任何模型、词典或外部资源。
对 ASCII 连续字母数字序列按词切分（小写化），对连续中文字符同时产出
character bigram 与 unigram，以兼顾长查询精度与短查询召回。

示例:
    "CPU使用率过高" ->
        ["cpu", "使用", "用率", "率过", "过高", "使", "用", "率", "过", "高"]
"""

from __future__ import annotations

__all__ = ["tokenize", "tokenize_for_fts"]

# CJK 统一表意文字主区段（含扩展 A）。用于判定“中文字符”。
_CJK_LO, _CJK_HI = 0x4E00, 0x9FFF
_CJK_EXT_A_LO, _CJK_EXT_A_HI = 0x3400, 0x4DBF


def _is_cjk(ch: str) -> bool:
    """判断单个字符是否属于常用/扩展 A 中文表意文字区段。"""
    cp = ord(ch)
    return (_CJK_LO <= cp <= _CJK_HI) or (_CJK_EXT_A_LO <= cp <= _CJK_EXT_A_HI)


def _is_ascii_alnum(ch: str) -> bool:
    """判断单个字符是否为 ASCII 字母或数字。"""
    return ch.isascii() and ch.isalnum()


def tokenize(text: str) -> list[str]:
    """将文本确定性地切分为 token 列表。

    - ASCII 连续字母数字序列作为一个 token（统一小写）。
    - 连续中文字符先产出相邻 bigram，再产出每个字的 unigram。
    - 标点、空白及其它符号被跳过。

    参数:
        text: 待分词文本。

    返回:
        token 列表；空文本返回空列表。
    """
    tokens: list[str] = []
    if not text:
        return tokens

    n = len(text)
    i = 0
    while i < n:
        ch = text[i]

        if _is_cjk(ch):
            # 收集一段连续中文字符。
            j = i
            while j < n and _is_cjk(text[j]):
                j += 1
            run = text[i:j]
            # bigram: 相邻两字组合。
            for k in range(len(run) - 1):
                tokens.append(run[k] + run[k + 1])
            # unigram: 单字，保证短查询可命中。
            tokens.extend(run)
            i = j

        elif _is_ascii_alnum(ch):
            # 连续 ASCII 字母/数字作为一个词，小写化。
            j = i
            while j < n and _is_ascii_alnum(text[j]):
                j += 1
            tokens.append(text[i:j].lower())
            i = j

        else:
            # 跳过标点、空白与符号。
            i += 1

    return tokens


def tokenize_for_fts(text: str) -> str:
    """将文本分词后返回空格分隔的 token 字符串，供 FTS5 插入与查询使用。"""
    return " ".join(tokenize(text))
