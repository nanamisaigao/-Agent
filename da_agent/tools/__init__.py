"""工具注册表。

新增工具只需三步：写一个 Tool，加进 TOOLS 元组，把描述写清楚。
主循环不认识具体工具，只认识 `schemas()` 和 `call()`。
"""

from __future__ import annotations

import json

from .base import Tool, ToolContext
from .sql import RUN_SQL

TOOLS: dict[str, Tool] = {tool.name: tool for tool in (RUN_SQL,)}


def schemas() -> list[dict]:
    """给模型看的工具清单。"""
    return [tool.schema() for tool in TOOLS.values()]


def call(name: str, arguments: str, ctx: ToolContext) -> dict:
    """执行一次工具调用。任何异常都转成结构化错误，绝不让主循环崩。"""
    tool = TOOLS.get(name)
    if tool is None:
        return {
            "ok": False,
            "error": f"未知工具：{name}",
            "hint": f"可用工具：{', '.join(TOOLS)}",
        }

    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"参数不是合法 JSON：{exc}",
            "hint": 'arguments 必须是 JSON 对象，例如 {"sql": "SELECT 1"}',
        }

    if not isinstance(args, dict):
        return {"ok": False, "error": "参数必须是 JSON 对象"}

    try:
        return tool.fn(args, ctx)
    except Exception as exc:  # 工具自己炸了，也不能拖垮循环
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


__all__ = ["Tool", "ToolContext", "TOOLS", "schemas", "call"]
