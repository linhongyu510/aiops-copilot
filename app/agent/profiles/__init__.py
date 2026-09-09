"""Domain Profile 抽象（P0-3）。

Profile 把「工具分组关键词、prompt 语气、告警字段映射」等硬编码值收敛为
可配置的领域档案。P0-3 阶段只做等价改造：内置 `aiops` profile 与旧硬编码
行为完全一致，不引入第二个 profile —— 等有真实第二场景（如 secops）时
再驱动 profile 抽象的进一步演化。

设计边界（避免过早抽象）：
- 只暴露 `get_active_profile()`，其他调用方不感知 Profile 类；
- Profile 未配置 → 回落到 tool_registry.py 里的原常量，行为不退步；
- 加载失败一律 warning + 使用内置默认值，绝不抛异常打断服务启动。
"""

from app.agent.profiles.loader import (
    DomainProfile,
    ProfileToolGroup,
    get_active_profile,
    load_profile,
    reset_profile_cache,
)

__all__ = [
    "DomainProfile",
    "ProfileToolGroup",
    "get_active_profile",
    "load_profile",
    "reset_profile_cache",
]
