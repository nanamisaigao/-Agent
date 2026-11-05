"""会话：多轮对话的存储 + 上下文压缩。

为什么单独一个模块：
多轮对话真正的难点不是"记住上一句"，是**历史会无限增长**。
一个五轮的分析，每轮都带几百行工具结果，很容易把预算吃光。
所以这里分成两件事：
    存（Session）  —— 完整历史落盘，用于回看和继续
    压（prepare）  —— 每次请求前按预算裁剪，只把需要的部分发给模型

压缩是**确定性**的（不调用模型），所以省一次请求、可预测、好测试：
    0 级：原样
    1 级：旧轮次丢掉工具调用和工具结果，只留"问题 + 结论"
    2 级：还不够，就把最老的几轮整体丢掉，压成一条摘要写进系统提示

注意 token 是**估算**的：中文按 1 字 1 token、其它按 3.5 字符 1 token。
系统提示 2959 字符实测 3563 token，说明中文确实接近 1:1，
用 len/4 估会严重低估。真实值每轮由 usage.prompt_tokens 校准。
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_BUDGET = 20_000      # 每次请求的上下文预算（tokens）
KEEP_FULL_TURNS = 2          # 最近 N 轮永不压缩

SESSIONS_DIR = "sessions"


def default_budget() -> int:
    """预算可以用环境变量调，比如 DA_CONTEXT_BUDGET=8000 省钱。"""
    try:
        return max(2_000, int(os.getenv("DA_CONTEXT_BUDGET", str(DEFAULT_BUDGET))))
    except ValueError:
        return DEFAULT_BUDGET


# --------------------------------------------------------------------------- #
# token 估算
# --------------------------------------------------------------------------- #

def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk + (len(text) - cjk) / 3.5) + 4


def message_tokens(message: dict) -> int:
    total = estimate_tokens(str(message.get("content") or ""))
    for call in message.get("tool_calls") or []:
        total += estimate_tokens(json.dumps(call.get("function", {}), ensure_ascii=False))
    return total


def messages_tokens(messages: list[dict]) -> int:
    return sum(message_tokens(m) for m in messages)


# --------------------------------------------------------------------------- #
# 会话存储
# --------------------------------------------------------------------------- #

@dataclass
class Session:
    id: str
    workspace: str
    title: str
    created_at: str
    updated_at: str
    messages: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    @property
    def turns(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "user")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace": self.workspace,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": self.messages,
            "usage": self.usage,
        }


def _dir(workspace) -> Path:
    path = Path(workspace.dir) / SESSIONS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(workspace, session_id: str) -> Path:
    return _dir(workspace) / f"{session_id}.json"


def new_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def make_title(question: str) -> str:
    text = " ".join(question.split())
    return text[:28] + ("…" if len(text) > 28 else "")


def create(workspace) -> Session:
    now = datetime.now().isoformat(timespec="seconds")
    return Session(id=new_id(), workspace=workspace.id, title="新对话",
                   created_at=now, updated_at=now)


def load(workspace, session_id: str) -> Session:
    path = _path(workspace, session_id)
    if not path.exists():
        raise FileNotFoundError(f"会话不存在：{session_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Session(
        id=data["id"],
        workspace=data.get("workspace", workspace.id),
        title=data.get("title") or "新对话",
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        messages=data.get("messages", []),
        usage=data.get("usage", {}),
    )


def save(workspace, session: Session) -> None:
    session.updated_at = datetime.now().isoformat(timespec="seconds")
    _path(workspace, session.id).write_text(
        json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")


def delete(workspace, session_id: str) -> None:
    path = _path(workspace, session_id)
    if path.exists():
        path.unlink()


def list_sessions(workspace) -> list[dict]:
    """会话列表。按更新时间倒序，只返回元数据。"""
    items = []
    for path in _dir(workspace).glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        items.append({
            "id": data.get("id", path.stem),
            "title": data.get("title", "未命名"),
            "updated_at": data.get("updated_at", ""),
            "turns": sum(1 for m in data.get("messages", []) if m.get("role") == "user"),
        })
    return sorted(items, key=lambda item: item["updated_at"], reverse=True)


# --------------------------------------------------------------------------- #
# 上下文压缩
# --------------------------------------------------------------------------- #

@dataclass
class Prepared:
    messages: list[dict]
    note: str = ""            # 压缩后追加到系统提示的说明
    tokens: int = 0
    dropped: int = 0          # 整体丢掉的轮数
    shrunk: int = 0           # 只保留结论的轮数


def split_turns(messages: list[dict]) -> list[list[dict]]:
    """按 user 消息切分轮次。"""
    turns: list[list[dict]] = []
    current: list[dict] | None = None
    for message in messages:
        if message.get("role") == "user":
            if current:
                turns.append(current)
            current = [message]
        else:
            if current is None:
                current = [message]
            else:
                current.append(message)
    if current:
        turns.append(current)
    return turns


def _conclusion(turn: list[dict]) -> str:
    """一轮里最后一段 assistant 文本，就是这轮给的结论。"""
    for message in reversed(turn):
        if message.get("role") == "assistant" and str(message.get("content") or "").strip():
            return str(message["content"]).strip()
    return ""


def _shrink(turn: list[dict]) -> list[dict]:
    """一级压缩：问题 + 结论，扔掉工具调用和原始数据。"""
    question = next((m for m in turn if m.get("role") == "user"), None)
    conclusion = _conclusion(turn)
    if question is None:
        return []
    out = [{"role": "user", "content": str(question.get("content") or "")}]
    if conclusion:
        out.append({"role": "assistant", "content": conclusion})
    return out


def prepare(history: list[dict], budget: int = DEFAULT_BUDGET,
            ratio: float = 1.0) -> Prepared:
    """按预算裁剪历史，返回可以直接接在新问题前面的 messages。

    ratio 是校准系数：用上一轮真实的 prompt_tokens 除以估算值，
    让后面的估算更准（不同模型的 tokenizer 差异很大）。
    """
    if not history:
        return Prepared(messages=[], tokens=0)

    turns = split_turns(history)
    total = int(messages_tokens(history) * ratio)
    if total <= budget or len(turns) <= KEEP_FULL_TURNS:
        return Prepared(messages=list(history), tokens=total, shrunk=0, dropped=0)

    # 一级：旧轮次只留结论
    rebuilt: list[list[dict]] = []
    shrunk = 0
    for index, turn in enumerate(turns):
        if index >= len(turns) - KEEP_FULL_TURNS:
            rebuilt.append(turn)
        else:
            slimmed = _shrink(turn)
            if slimmed:
                rebuilt.append(slimmed)
                shrunk += 1

    messages = [m for turn in rebuilt for m in turn]
    total = int(messages_tokens(messages) * ratio)
    if total <= budget or len(rebuilt) <= KEEP_FULL_TURNS:
        note = ""
        if shrunk:
            note = f"（前面 {shrunk} 轮对话已压缩：只保留问题和结论，中间的工具调用与原始数据已省略）"
        return Prepared(messages=messages, tokens=total, shrunk=shrunk, note=note)

    # 二级：从最老的开始整轮丢弃
    dropped_turns = rebuilt[: len(rebuilt) - KEEP_FULL_TURNS]
    kept = rebuilt[len(rebuilt) - KEEP_FULL_TURNS:]
    lines = []
    for turn in dropped_turns:
        question = str(turn[0].get("content") or "")[:60]
        conclusion = _conclusion(turn)[:80]
        lines.append(f"- 问过：{question}" + (f" → 结论：{conclusion}" if conclusion else ""))

    messages = [m for turn in kept for m in turn]
    total = int(messages_tokens(messages) * ratio)
    note = "## 更早的对话（已压缩，仅供参考）\n" + "\n".join(lines)

    return Prepared(messages=messages, tokens=total, shrunk=shrunk,
                    dropped=len(dropped_turns), note=note)
