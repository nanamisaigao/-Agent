"""工具基类与调用上下文。

一个工具 = 名字 + 描述 + JSON Schema + 执行函数。
其中 **description 是写给模型看的**，它直接决定模型会不会选对工具、参数填不填对。

ToolContext 是"这次调用针对哪个数据库"。为什么要单独传：
工具函数不读全局变量，这样同一个进程里可以同时服务多个工作区，
测试时也能直接塞一个临时库进去。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ToolContext:
    db_path: Path
    workspace_id: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[dict, ToolContext], dict]

    def schema(self) -> dict:
        """转成 OpenAI 的 tools 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
