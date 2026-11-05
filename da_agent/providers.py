"""常用模型服务商的预设。

**这些只是起点，不是真理。** 各家的地址和模型名会变，所以：
1. 界面上填好后可以随手改；
2. 有「获取模型列表」按钮，会拿你的 key 去问服务端 `/models`，
   返回的才是当下真实可用的 ID。

base_url 里不要带 /chat/completions —— 代码自己会拼。
checked 是人工核对日期，方便以后判断这批预设有多旧。
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHECKED = "2026-11"


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    models: tuple[str, ...] = ()
    key_url: str = ""
    note: str = ""
    thinking: str | None = None      # 只有 DeepSeek 需要这个开关

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "models": list(self.models),
            "key_url": self.key_url,
            "note": self.note,
            "thinking": self.thinking,
        }


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        models=("deepseek-flash", "deepseek-v4-pro"),
        key_url="https://platform.deepseek.com/api_keys",
        note="便宜、工具调用稳，本项目默认用它",
        thinking="disabled",
    ),
    Provider(
        id="siliconflow",
        name="硅基流动",
        base_url="https://api.siliconflow.cn/v1",
        models=("Qwen/Qwen3-8B", "deepseek-ai/DeepSeek-V3"),
        key_url="https://cloud.siliconflow.cn/account/ak",
        note="一个 key 用多家开源模型，有免费额度",
    ),
    Provider(
        id="dashscope",
        name="阿里云百炼（千问）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=("qwen-plus", "qwen-max", "qwen-turbo"),
        key_url="https://bailian.console.aliyun.com/",
        note="新账号建议用业务空间专属域名，这里给的是通用域名",
    ),
    Provider(
        id="zhipu",
        name="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        models=("glm-5.3", "glm-5.3-flash"),
        key_url="https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
        note="GLM 系列，带思考模式",
    ),
    Provider(
        id="moonshot",
        name="Kimi（月之暗面）",
        base_url="https://api.moonshot.cn/v1",
        models=("kimi-k3", "kimi-k3.1-pro"),
        key_url="https://platform.kimi.com/console/api-keys",
        note="长上下文见长",
    ),
    Provider(
        id="mimo",
        name="小米 MiMo",
        base_url="https://api.xiaomimimo.com/v1",
        models=("mimo-v2.6-pro",),
        key_url="https://platform.xiaomimimo.com/",
        note="小米自研模型，OpenAI 兼容",
    ),
    Provider(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        models=("gpt-4o-mini",),
        key_url="https://platform.openai.com/api-keys",
        note="需要海外支付方式，价格比国内贵不少",
    ),
    Provider(
        id="ollama",
        name="本地 Ollama",
        base_url="http://127.0.0.1:11434/v1",
        models=("qwen3:8b", "llama3.2"),
        key_url="",
        note="完全离线免费；key 随便填（ollama 不校验），工具调用能力看模型",
    ),
    Provider(
        id="custom",
        name="其他 / 自定义",
        base_url="",
        models=(),
        note="任何 OpenAI 兼容服务，自己填地址和模型名",
    ),
)


def to_dicts() -> list[dict]:
    return [provider.to_dict() for provider in PROVIDERS]
