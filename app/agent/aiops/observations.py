"""观测压缩（P0.4）：工具原始输出 → 结构化上下文。

问题：Executor 把工具原始输出整段塞进 past_steps，Replanner 决策时再
硬截断到 300 字符——决策看不清，最终报告又拿不到完整证据。

方案：
- past_steps（决策上下文）：确定性 head+tail 压缩，保头保尾并标注省略量；
- artifacts（执行存档）：按步骤保留完整原始输出（同样设上限防失控），
  最终报告生成时优先引用 artifacts，还原完整证据链；
- 全程无 LLM 参与，确定性、零成本、可单测。
"""

from __future__ import annotations


def compact_observation(text: str, max_chars: int) -> str:
    """把超长观测文本压缩为 head + 省略标记 + tail。

    头部通常含查询条件与首批数据，尾部通常含汇总/错误信息，
    两端保留比任意截断更能维持诊断线索的完整性。
    """
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head_budget = max(int(max_chars * 0.6), 1)
    tail_budget = max(int(max_chars * 0.3), 0)
    head = text[:head_budget]
    tail = text[len(text) - tail_budget :] if tail_budget else ""
    omitted = len(text) - len(head) - len(tail)
    marker = f"\n[…观测结果过长，已省略中间 {omitted} 字符，完整内容见执行存档…]\n"
    return head + marker + tail
