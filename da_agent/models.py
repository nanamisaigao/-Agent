"""模型档案：保存多套模型配置，随时切换。

存在 `config/models.json`，结构：

    {"active": "<id>", "profiles": [{"id","name","base_url","model","api_key","thinking"}]}

**为什么明文存**：这是个本地单机工具，加密得再管理一把密钥，反而更麻烦。
代价是任何能读这个文件的人都能拿到 key，所以 `config/` 必须进 .gitignore。
真要更安全，就只在会话里填、不落盘（那是另一种产品）。

优先级：界面保存的档案 > .env 环境变量。
没有任何档案时自动回落到 .env，所以老用户升级后不会突然不能用。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from .config import MODELS_FILE, LLMSettings, llm_settings


@dataclass
class ModelProfile:
    id: str
    name: str
    base_url: str
    model: str
    api_key: str
    thinking: str | None = None

    def masked(self) -> dict:
        """给前端看的版本：绝不带出 api_key。"""
        key = self.api_key or ""
        hint = f"{key[:3]}…{key[-4:]}" if len(key) >= 8 else ("已设置" if key else "未设置")
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "thinking": self.thinking,
            "key_hint": hint,
        }

    def settings(self) -> LLMSettings:
        return LLMSettings(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            timeout=60.0,
            thinking=self.thinking,
            label=self.name,
        )


# --------------------------------------------------------------------------- #
# 存取
# --------------------------------------------------------------------------- #

def _read() -> dict:
    if not MODELS_FILE.exists():
        return {"active": None, "profiles": []}
    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"active": None, "profiles": []}
    data.setdefault("profiles", [])
    data.setdefault("active", None)
    return data


def _write(data: dict) -> None:
    MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODELS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _slug(name: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z_-]+", "-", name.strip()).strip("-").lower()
    return slug or "model"


def list_profiles() -> list[ModelProfile]:
    return [ModelProfile(**item) for item in _read()["profiles"]]


def get(profile_id: str) -> ModelProfile | None:
    for profile in list_profiles():
        if profile.id == profile_id:
            return profile
    return None


def add(name: str, base_url: str, model: str, api_key: str,
        thinking: str | None = None) -> ModelProfile:
    data = _read()
    profile_id = _slug(name)
    existing = {item["id"] for item in data["profiles"]}
    if profile_id in existing:
        profile_id = f"{profile_id}-{datetime.now():%H%M%S}"

    profile = ModelProfile(id=profile_id, name=name.strip() or profile_id,
                           base_url=base_url.strip().rstrip("/"),
                           model=model.strip(), api_key=api_key.strip(),
                           thinking=thinking)
    data["profiles"].append(asdict(profile))
    data["active"] = profile.id
    _write(data)
    return profile


def activate(profile_id: str) -> None:
    data = _read()
    if not any(item["id"] == profile_id for item in data["profiles"]):
        raise KeyError(f"模型不存在：{profile_id}")
    data["active"] = profile_id
    _write(data)


def remove(profile_id: str) -> None:
    data = _read()
    data["profiles"] = [item for item in data["profiles"] if item["id"] != profile_id]
    if data.get("active") == profile_id:
        data["active"] = data["profiles"][0]["id"] if data["profiles"] else None
    _write(data)


def active_profile() -> ModelProfile | None:
    data = _read()
    if not data.get("active"):
        return None
    return get(data["active"])


def current_settings() -> LLMSettings:
    """当前该用哪套配置：档案优先，没有档案就用 .env。"""
    profile = active_profile()
    return profile.settings() if profile else llm_settings()


def public_state() -> dict:
    """给前端的整体状态（不含密钥）。"""
    data = _read()
    return {
        "active": data.get("active"),
        "items": [profile.masked() for profile in list_profiles()],
        "fallback": None if active_profile() else {
            "model": llm_settings().model,
            "base_url": llm_settings().base_url,
            "has_key": bool(llm_settings().api_key),
        },
    }
