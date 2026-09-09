"""Skill 验收表达式求值（零依赖版本）。

从 ``app.agent.skills.verifier`` 抽出，字面复制。只用 stdlib.ast，安全白名单。
"""

from __future__ import annotations

import ast
from typing import Any

from loguru import logger

_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Attribute,
    ast.Subscript,
    ast.Index,  # py<3.9 保留；py>=3.9 已 deprecate 但 ast 仍认
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Set,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.USub,
    ast.UAdd,
)


class _SafeEnv(dict):
    def __getattr__(self, item: str) -> Any:
        if item.startswith("__"):
            raise AttributeError(item)
        if item in self:
            value = self[item]
            if isinstance(value, dict) and not isinstance(value, _SafeEnv):
                value = _SafeEnv(value)
            return value
        return None


def _wrap(value: Any) -> Any:
    if isinstance(value, dict) and not isinstance(value, _SafeEnv):
        return _SafeEnv({k: _wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value


def evaluate_expr(expr: str, env: dict[str, Any]) -> Any:
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"表达式包含不允许的节点: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError(f"表达式名字非法: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError(f"表达式属性非法: {node.attr}")

    wrapped = {key: _wrap(value) for key, value in env.items()}
    return eval(  # noqa: S307 - 白名单 AST 已校验
        compile(tree, "<skill-verify>", "eval"),
        {"__builtins__": {}},
        wrapped,
    )


def evaluate_success_expr(expr: str, result: Any, context: dict[str, Any]) -> bool:
    if not (expr or "").strip():
        return False
    env: dict[str, Any] = {"result": result, "context": context}
    if isinstance(result, dict):
        for key, value in result.items():
            env.setdefault(key, value)
    try:
        outcome = evaluate_expr(expr, env)
    except Exception as exc:
        logger.warning(f"verification 表达式求值失败 expr={expr!r}: {exc}")
        return False
    return bool(outcome)
