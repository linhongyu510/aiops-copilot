"""工具安全层（P1.2）：Prompt 注入防御 + 工具级 RBAC。

两个此前缺失的治理能力收口在 MCP 客户端拦截器（所有工具调用的唯一咽喉）：

1. 工具级 RBAC：此前角色只控制 HTTP 端点，进入 Agent 层后 viewer 与
   admin 能力完全相同。现在每次工具调用前按工具注册元数据
   （tool_registry.spec_for(name).required_role）校验当前角色
   （HTTP 中间件注入 / 后台任务显式声明）；
2. 注入防御：工具输出与联网检索结果此前原文拼进消息历史，恶意内容
   可以携带「忽略之前的指令」类提示词劫持。现在输出统一围栏包裹
   （标记为数据而非指令）并按行清除常见注入模式。
   注入模式表由「基础模式（本模块常量）+ profile 追加模式（C7）」合成，
   便于 SecOps 等特化 profile 追加 curl|sh、命令代入等领域注入模式。

拒绝与清洗都以 isError/文本形式返回，绝不中断诊断主流程。
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar

from loguru import logger
from mcp.types import CallToolResult, TextContent

from app.agent.tool_registry import tool_registry
from app.security import ROLE_LEVEL

# 当前请求/任务的角色上下文。
# - HTTP 请求：main.operational_middleware 在鉴权后注入；
# - 后台任务（incident 自治诊断）：创建任务的上下文继承（webhook 需 operator）；
# - 兜底默认 operator：全部只读工具（viewer）可用，web_search（operator）可用，
#   未来 admin 级高危变更工具在无身份上下文时不可用（最小权限）。
_current_role: ContextVar[str] = ContextVar("aiops_current_role", default="operator")

# 常见提示词注入模式（span 粒度匹配；短行整行清除，长数据行仅清除命中片段）
_BASE_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore\s+(all\s+|everything\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)"),
    re.compile(r"(?i)disregard\s+(all\s+|everything\s+)?(previous|prior|above|everything)"),
    re.compile(r"(?i)\bsystem\s*[:：]"),
    re.compile(r"(?i)reveal\s+(your|the)\s+(system\s+)?prompt"),
    re.compile(r"(?i)you\s+are\s+now\s+a\b"),
    re.compile(r"(?i)jailbreak|developer\s+mode|god\s+mode"),
    re.compile(r"忽略(之前|以上|上面|先前|所有)的?(指令|提示|要求|设定)?"),
    re.compile(r"(你现在|从现在起)(?:你)?(?:是|扮演)一个"),
    re.compile(r"请(务必|一定要)?(执行|运行)(以下|下列)?(命令|脚本|变更)"),
    re.compile(r"(?i)execute\s+the\s+following\s+(command|script|mutation)"),
)


def _profile_injection_patterns() -> tuple[re.Pattern[str], ...]:
    """从 active profile 加载追加的注入模式；解析失败或未配置返回空元组。"""
    try:
        from app.agent.profiles import get_active_profile

        raw_patterns = get_active_profile().injection_patterns
    except Exception:  # noqa: BLE001 - profile 层任何异常都不能拖垮清洗
        return ()
    compiled: list[re.Pattern[str]] = []
    for raw in raw_patterns:
        try:
            compiled.append(re.compile(raw))
        except re.error as exc:
            logger.warning(f"Profile 注入模式编译失败，已跳过: {raw!r} ({exc})")
    return tuple(compiled)


def _active_injection_patterns() -> tuple[re.Pattern[str], ...]:
    """基础模式 + profile 追加模式（C7）。profile 未配置时等价于基础模式。"""
    return _BASE_INJECTION_PATTERNS + _profile_injection_patterns()


# 兼容旧引用：保留原名，指向基础模式（外部读者应改用 `_active_injection_patterns()`）
_INJECTION_LINE_PATTERNS = _BASE_INJECTION_PATTERNS

# 纯注入行判据：命中片段的字符占比 ≥ 该值的行视为纯注入行，整行替换；
# 占比低的行（注入嵌在长数据中）走片段级替换，避免「清洗毁数据」
_PURE_INJECTION_LINE_RATIO = 0.6

INJECTION_PLACEHOLDER = "[已移除疑似提示词注入内容]"


def set_current_role(role: str):
    """设置当前任务的角色上下文（HTTP 中间件/后台任务入口调用）。

    返回 ContextVar Token，供请求结束时 reset，防止上下文串号。
    """
    if role in ROLE_LEVEL:
        return _current_role.set(role)
    return None


def reset_current_role(token) -> None:
    if token is not None:
        _current_role.reset(token)


def get_current_role() -> str:
    return _current_role.get()


@contextmanager
def role_context(role: str):
    """临时切换角色上下文（测试与后台任务入口使用）。"""
    token = set_current_role(role)
    try:
        yield
    finally:
        reset_current_role(token)


def check_tool_permission(tool_name: str, role: str | None = None) -> tuple[bool, str]:
    """校验当前角色能否调用指定工具；返回 (是否允许, 原因文本)。"""
    current = role or get_current_role()
    spec = tool_registry.spec_for(tool_name)
    required = spec.required_role
    if ROLE_LEVEL.get(current, 0) >= ROLE_LEVEL.get(required, 10_000):
        return True, ""
    reason = (
        f"权限不足：当前角色 {current} 无法调用工具 {tool_name}"
        f"（需要 {required}）。请向用户说明该操作需要更高权限，不要尝试绕过。"
    )
    logger.warning(f"工具级 RBAC 拒绝: {tool_name} 需要 {required}，当前 {current}")
    return False, reason


def sanitize_tool_output(tool_name: str, text: str, max_chars: int) -> str:
    """围栏包裹工具输出并清除疑似注入内容。

    混合清除策略：
    - 短行（≤200 字符，典型为独立注入行）整行替换，防御最强；
    - 长数据行（如单行 JSON/宽日志）只替换命中的片段，保留其余观测数据，
      避免「清洗毁数据」的误伤。
    """
    cleaned_lines = []
    removed = 0
    patterns = _active_injection_patterns()
    for line in text.splitlines():
        matches = [
            match
            for pattern in patterns
            for match in pattern.finditer(line)
        ]
        if not matches:
            cleaned_lines.append(line)
            continue
        # 命中片段占整行的字符比例：纯注入行整行清除，数据行只清命中片段
        matched_chars = sum(match.end() - match.start() for match in matches)
        if matched_chars / max(len(line), 1) >= _PURE_INJECTION_LINE_RATIO:
            cleaned_lines.append(INJECTION_PLACEHOLDER)
            removed += 1
            continue
        # 长行：按命中区间切片替换（去重叠）
        matches.sort(key=lambda match: match.start())
        pieces: list[str] = []
        cursor = 0
        for match in matches:
            if match.start() < cursor:
                continue
            pieces.append(line[cursor : match.start()])
            pieces.append(INJECTION_PLACEHOLDER)
            cursor = match.end()
            removed += 1
        pieces.append(line[cursor:])
        cleaned_lines.append("".join(pieces))
    body = "\n".join(cleaned_lines)
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars] + f"\n[…工具输出超长，已截断至 {max_chars} 字符…]"
    if removed:
        logger.info(f"工具 {tool_name} 输出清除疑似注入片段 {removed} 处")
    return (
        f"<tool_output name=\"{tool_name}\">\n"
        f"{body}\n"
        f"</tool_output>"
    )


def sanitize_call_tool_result(tool_name: str, result: CallToolResult, max_chars: int) -> CallToolResult:
    """对 CallToolResult 的文本内容做注入清洗（错误结果原样返回）。"""
    if result.isError:
        return result
    sanitized_content = []
    for block in result.content:
        if isinstance(block, TextContent) and block.text:
            sanitized_content.append(
                TextContent(
                    type="text",
                    text=sanitize_tool_output(tool_name, block.text, max_chars),
                )
            )
        else:
            sanitized_content.append(block)
    return CallToolResult(content=sanitized_content, isError=result.isError)


async def governance_interceptor(request, handler):
    """MCP 治理拦截器：权限校验 → 变更提案拦截 → 输出注入清洗。

    挂载顺序在 retry_interceptor 之外层：拒绝与提案拦截直接返回，不消耗重试预算。
    """
    from app.agent.action_governor import action_governor
    from app.config import config

    allowed, reason = check_tool_permission(request.name)
    if not allowed:
        return CallToolResult(
            content=[TextContent(type="text", text=reason)], isError=True
        )

    # 变更类工具（risk_level > 0）不执行，生成提案等待人工审批（P1.3）
    proposal = await action_governor.evaluate(
        request.name, getattr(request, "args", None)
    )
    if proposal is not None:
        return CallToolResult(
            content=[TextContent(type="text", text=proposal.to_prompt_text())],
            isError=False,
        )

    result = await handler(request)
    try:
        return sanitize_call_tool_result(
            request.name, result, config.tool_output_max_chars
        )
    except Exception as exc:
        logger.warning(f"工具输出清洗失败（返回原始结果）: {exc}")
        return result
