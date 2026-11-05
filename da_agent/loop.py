"""主循环：整个项目的心脏。

它是"一个内核，多个前端"里的内核：只负责推理和调用工具，
把所有过程作为事件（纯 JSON）yield 出去。CLI、Web、Notebook 各自渲染。

循环长这样：

    用户问题
      └→ 调模型（带工具清单）
           ├→ 模型要调工具 → 执行 → 结果塞回 messages → 回到调模型
           └→ 模型给出文字 → 结束

要理解的三件事：

1. **会话状态就是 messages 这一个列表。** 模型没有记忆，每次都是把整个
   messages 重新发一遍。所以上下文管理 = 你怎么往这个列表里塞东西。

2. **工具结果必须原样回灌。** 包括错误和 hint。模型是靠看到报错才会改 SQL 的，
   如果你在代码里把错误吞掉，它就永远改不对。

3. **必须有步数上限。** 模型会打转：同一个错 SQL 换个写法再试、再错、再试。
   这是成本事故的主要来源。
"""

from __future__ import annotations

import json
from typing import Iterator, Protocol

from .context import build_system_prompt
from .llm import LLMError, LLMReply, chat
from .tools import call as call_tool
from .tools import schemas


class ChatClient(Protocol):
    """任何实现 complete() 的对象都能当模型用（含离线假模型）。"""

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply: ...


class ApiClient:
    """真实模型。"""

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply:
        return chat(messages, tools)


def _assistant_message(reply: LLMReply) -> dict:
    """把模型的回复原样转成 messages 里的一条。

    工具调用必须按 OpenAI 的格式回填，否则下一轮请求会被服务端拒绝。
    """
    message: dict = {"role": "assistant", "content": reply.content or ""}
    # DeepSeek 在思考模式下要求把 reasoning_content 原样带回去，否则下一轮 400。
    # 关掉思考模式时这里是 None，不会出现在请求里。
    if reply.reasoning:
        message["reasoning_content"] = reply.reasoning
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in reply.tool_calls
        ]
    return message


def _parse_arguments(raw: str) -> dict | str:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else raw
    except json.JSONDecodeError:
        return raw


def run(question: str, client: ChatClient | None = None,
        max_steps: int = 8, system_prompt: str | None = None) -> Iterator[dict]:
    """执行一次问答，逐个 yield 事件。

    事件类型：run_start / text / tool_call / tool_result / answer / run_end / error
    """
    client = client or ApiClient()
    messages: list[dict] = [
        {"role": "system", "content": system_prompt or build_system_prompt()},
        {"role": "user", "content": question},
    ]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}

    yield {"type": "run_start", "question": question}

    for step in range(1, max_steps + 1):
        try:
            reply = client.complete(messages, schemas())
        except LLMError as exc:
            yield {"type": "error", "message": str(exc)}
            yield {"type": "run_end", "ok": False, "steps": step - 1,
                   "usage": usage, "messages": messages}
            return

        usage["prompt_tokens"] += int(reply.usage.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(reply.usage.get("completion_tokens") or 0)

        # 只有"还要继续调工具"时的文字才算中间叙述；
        # 没有工具调用时，这段文字就是最终回答，由 answer 事件承载，避免重复渲染
        if reply.content and reply.tool_calls:
            yield {"type": "text", "text": reply.content}

        if not reply.tool_calls:
            yield {"type": "answer", "text": reply.content or "（模型没有给出文字回答）"}
            yield {"type": "run_end", "ok": True, "steps": step,
                   "usage": usage, "messages": messages}
            return

        messages.append(_assistant_message(reply))

        for call in reply.tool_calls:
            arguments = _parse_arguments(call["arguments"])
            yield {"type": "tool_call", "id": call["id"],
                   "name": call["name"], "args": arguments, "step": step}

            result = call_tool(call["name"], call["arguments"])

            yield {"type": "tool_result", "id": call["id"], "name": call["name"],
                   "result": result, "step": step}

            # 原样回灌：包括 error 和 hint。模型能不能自愈，全看这里。
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })

    yield {"type": "error", "message": f"超过最大步数 {max_steps}，任务未收敛（可能在打转）"}
    yield {"type": "run_end", "ok": False, "steps": max_steps,
           "usage": usage, "messages": messages}
