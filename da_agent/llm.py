"""模型通道：OpenAI 兼容的 /chat/completions。

为什么手写 HTTP 而不是装 openai SDK：
1. 零新依赖，不用再折腾 pip（沙箱装包挺麻烦的）；
2. 这个协议的本质就是"POST 一段 JSON，拿回一段 JSON"。自己写一遍，
   以后换任何一家兼容服务，你都知道该改哪三个地方（url / headers / model）；
3. 出问题时能直接打印请求和响应原文，而不是和 SDK 的异常类型搏斗。

接口约定：任何客户端都实现 `complete(messages, tools) -> LLMReply`。
主循环只认这个接口，所以测试时能塞一个假的进去，不花一分钱。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import llm_settings


class LLMError(RuntimeError):
    """调用模型失败。消息要能直接给人看。"""


@dataclass
class LLMReply:
    content: str | None = None
    tool_calls: list[dict] = field(default_factory=list)   # [{"id","name","arguments"}]
    usage: dict = field(default_factory=dict)             # prompt/completion/total tokens
    finish_reason: str | None = None
    reasoning: str | None = None                          # 思维链（DeepSeek 的 reasoning_content）


def _parse(data: dict) -> LLMReply:
    if isinstance(data.get("error"), dict) and data["error"]:
        raise LLMError(str(data["error"])[:400])
    if not data.get("choices"):
        raise LLMError(f"响应里没有 choices：{str(data)[:300]}")

    choice = data["choices"][0]
    message = choice.get("message") or {}
    calls = [
        {
            "id": call.get("id", ""),
            "name": call["function"]["name"],
            "arguments": call["function"].get("arguments") or "{}",
        }
        for call in (message.get("tool_calls") or [])
    ]
    return LLMReply(
        content=message.get("content"),
        tool_calls=calls,
        usage=data.get("usage") or {},
        finish_reason=choice.get("finish_reason"),
        reasoning=message.get("reasoning_content"),
    )


def chat(messages: list[dict], tools: list[dict] | None = None,
         settings=None, attempts: int = 3) -> LLMReply:
    """调用 /chat/completions。429 / 5xx / 网络错误会退避重试。

    settings 为空时回落到环境变量——这样命令行和网页可以各用各的配置。
    """
    settings = settings or llm_settings()

    if not settings.api_key:
        raise LLMError(
            "没有找到 API key。请在项目根目录的 .env 里写：\n"
            "  OPENAI_API_KEY=sk-...\n"
            "或者设置环境变量 OPENAI_API_KEY / DA_LLM_API_KEY。\n"
            "（.env 要加进 .gitignore，别把密钥提交上去）"
        )

    url = settings.base_url.rstrip("/") + "/chat/completions"
    payload: dict = {"model": settings.model, "messages": messages, "temperature": 0}

    # DeepSeek 默认开启"思考模式"，会多返回一个 reasoning_content，
    # 而且带 tools 的请求必须把历史 reasoning_content 原样回传，否则 400。
    # 对工具调用类任务，思考模式既慢又贵，默认关掉。
    # 注意：这个字段不是 OpenAI 标准的，所以只在明确需要时才发出去。
    thinking = settings.thinking if settings.thinking is not None else os.getenv("DA_LLM_THINKING")
    if thinking is None and "deepseek" in settings.base_url:
        thinking = "disabled"
    if thinking in ("enabled", "disabled"):
        payload["thinking"] = {"type": thinking}

    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.api_key}",
        "User-Agent": "da-agent/0.1",
    }

    last_error: LLMError | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=body, method="POST", headers=headers)
            with urllib.request.urlopen(request, timeout=settings.timeout) as response:
                return _parse(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (408, 409, 429) or exc.code >= 500:
                last_error = LLMError(f"HTTP {exc.code}：{detail}")
                time.sleep(2 * (attempt + 1))
                continue
            raise LLMError(f"HTTP {exc.code}：{detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = LLMError(f"网络错误：{exc}")
            time.sleep(2 * (attempt + 1))

    raise last_error or LLMError("调用模型失败（未知原因）")


# --------------------------------------------------------------------------- #
# 离线演示客户端：不联网也能验证整个循环
# --------------------------------------------------------------------------- #

class EchoClient:
    """假模型：第一轮固定发一条 run_sql，第二轮把工具结果复述出来。

    用途：
    1. 没有 API key 时验证循环、事件流、CLI 渲染是否正常；
    2. 以后写单测时当桩用，避免测试依赖网络。
    """

    def __init__(self, sql: str = "SELECT COUNT(*) AS orders FROM orders") -> None:
        self.sql = sql
        self.calls = 0

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply:
        self.calls += 1
        if messages[-1]["role"] == "tool":
            return LLMReply(
                content=f"（离线演示）查询结果是：{messages[-1]['content'][:300]}",
                usage={"prompt_tokens": 0, "completion_tokens": 0},
                finish_reason="stop",
            )
        return LLMReply(
            content=None,
            tool_calls=[{"id": "call_demo_1", "name": "run_sql",
                         "arguments": json.dumps({"sql": self.sql})}],
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            finish_reason="tool_calls",
        )


__all__ = ["LLMError", "LLMReply", "EchoClient", "chat"]
