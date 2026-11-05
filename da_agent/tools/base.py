"""工具基类。

一个工具 = 名字 + 描述 + JSON Schema + 执行函数。
其中 **description 是写给模型看的**，它直接决定模型会不会选对工具、参数填不填对。
写工具描述时，要当成"给新人写接口文档"来写。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[dict], dict]

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
