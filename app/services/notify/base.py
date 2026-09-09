"""通知渠道抽象基类（P1-1）。

`Notifier.notify(incident)` 是唯一契约：
- 每个渠道独立处理错误（不允许抛出，异常必须自吞并 log）；
- 支持简单的启用开关（`enabled=False` 时静默跳过）；
- 通知内容为 `Incident.to_dict()`，避免各渠道各自决定字段。

不引入插件加载机制：内置三种渠道（webhook/slack/feishu）通过 `build_notifiers()`
按 `config.incident_notifiers` 组装即可；等真正接入第 4 个渠道再看是否需要动态注册。
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class Notifier:
    """通知渠道基类：子类必须实现 `_deliver`；`notify` 已封好错误隔离。"""

    #: 稳定标识（用于日志、配置解析、指标标签）。
    name: str = "base"

    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled

    async def notify(self, incident: dict[str, Any]) -> bool:
        """发送通知；返回是否成功。异常自吞，绝不冒泡到 IncidentService。"""
        if not self.enabled:
            logger.debug(f"[notify:{self.name}] disabled, skip")
            return False
        try:
            await self._deliver(incident)
            return True
        except NotImplementedError:
            # 骨架渠道未接入：明确 warn，不当作失败干扰调用方统计
            logger.warning(
                f"[notify:{self.name}] 未接入，跳过（补齐配置后自动生效）"
            )
            return False
        except Exception as exc:
            logger.warning(f"[notify:{self.name}] 发送失败: {exc}")
            return False

    async def _deliver(self, incident: dict[str, Any]) -> None:
        raise NotImplementedError
