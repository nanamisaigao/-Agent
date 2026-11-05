"""Web 服务：把 loop 的事件流用 SSE 推给浏览器，并提供工作区/模型的管理接口。

设计要点：

1. **复用同一个 loop.run()，业务逻辑一行不重写。**
   这就是"一个内核，多个前端"：CLI 是渲染器，浏览器也是渲染器。

2. **SSE 包一个同步生成器。**
   我们的模型调用是阻塞式 urllib，放进 async def 会卡死事件循环。
   Starlette 对同步生成器会自动放到线程池里跑，正好合用。

3. **每个请求绑定一个工作区 + 一套模型配置。**
   工作区决定"查哪个库、用哪份字典"；模型配置决定"谁来回答"。
"""

from __future__ import annotations

import json
from typing import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import models
from . import session as session_store
from .config import ARTIFACTS_DIR, PROJECT_ROOT
from .llm import EchoClient
from .loop import ApiClient, run
from .trace import write_trace
from .workspace import create, current, list_all, load, set_active

WEB_DIR = PROJECT_ROOT / "web"

app = FastAPI(title="da_agent", docs_url="/api/docs")


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #

class AskRequest(BaseModel):
    question: str
    session_id: str | None = None
    max_steps: int = 8
    fake: bool = False
    workspace: str | None = None


class WorkspaceIn(BaseModel):
    path: str
    name: str | None = None


class IdIn(BaseModel):
    id: str


class ModelIn(BaseModel):
    name: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: str = Field(min_length=1)
    thinking: str | None = None


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _sse(payload: dict | str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, default=str)
    return f"data: {data}\n\n"


def _wire(event: dict) -> dict:
    """发到网络的版本：去掉 messages。

    run_end 里的 messages 是给 trace 复盘用的（含整个系统提示，几十 KB），
    浏览器只需要 usage 和步数。不裁剪的话每一轮都在白传几万字符。
    """
    if event.get("type") == "run_end":
        return {key: value for key, value in event.items() if key != "messages"}
    return event


# --------------------------------------------------------------------------- #
# 提问
# --------------------------------------------------------------------------- #

@app.post("/api/ask")
def ask(req: AskRequest) -> StreamingResponse:
    def stream() -> Iterator[str]:
        records: list[dict] = []
        try:
            workspace = load(req.workspace) if req.workspace else current()
            session = (session_store.load(workspace, req.session_id)
                       if req.session_id else session_store.create(workspace))
            client = EchoClient() if req.fake else ApiClient(models.current_settings())

            for event in run(req.question, client=client, max_steps=req.max_steps,
                             workspace=workspace, history=session.messages):
                if event["type"] == "run_start":
                    event["session_id"] = session.id
                    event["session_title"] = session.title

                if event["type"] == "run_end" and event.get("ok"):
                    # 只保存成功的轮次：失败的那一轮没有 assistant 消息，
                    # 存下来会在下次请求里留下一条"只有提问没有回答"的悬空历史
                    session.messages = event["messages"][1:]      # 去掉 system
                    if session.title in ("", "新对话"):
                        session.title = session_store.make_title(req.question)
                    for key in ("prompt_tokens", "completion_tokens"):
                        session.usage[key] = session.usage.get(key, 0) + int(
                            (event.get("usage") or {}).get(key) or 0)
                    session_store.save(workspace, session)

                records.append(event)
                yield _sse(_wire(event))
        except Exception as exc:            # 任何没收住的异常都要变成事件，不能断流
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

        write_trace(records)
        yield _sse("[DONE]")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #

@app.get("/api/workspaces")
def workspaces() -> dict:
    items = []
    for meta in list_all():
        workspace = load(meta["id"])
        items.append({
            "id": workspace.id,
            "name": workspace.name,
            "tables": workspace.tables,
            "rows": workspace.rows,
            "db": str(workspace.db),
        })
    try:
        active = current().id
    except Exception:
        active = items[0]["id"] if items else None
    return {"active": active, "items": items}


@app.post("/api/workspaces")
def add_workspace(body: WorkspaceIn) -> dict:
    from pathlib import Path

    try:
        workspace = create(Path(body.path).expanduser(), name=body.name)
    except FileNotFoundError:
        raise HTTPException(400, f"找不到这个文件：{body.path}") from None
    except Exception as exc:
        raise HTTPException(400, f"分析失败：{exc}") from None

    set_active(workspace.id)
    return {"id": workspace.id, "name": workspace.name,
            "tables": workspace.tables, "rows": workspace.rows}


@app.post("/api/workspaces/activate")
def activate_workspace(body: IdIn) -> dict:
    try:
        set_active(body.id)
    except Exception:
        raise HTTPException(404, f"工作区不存在：{body.id}") from None
    return {"ok": True, "active": body.id}


@app.get("/api/tables")
def tables(workspace: str | None = None) -> list[dict]:
    ws = load(workspace) if workspace else current()
    return [
        {
            "name": table["name"],
            "rows": table["rows"],
            "note": table.get("note", ""),
            "columns": [{"name": c["name"], "type": c["type"]} for c in table["columns"]],
        }
        for table in ws.catalog.get("tables", [])
    ]


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #

@app.get("/api/sessions")
def list_sessions(workspace: str | None = None) -> list[dict]:
    ws = load(workspace) if workspace else current()
    return session_store.list_sessions(ws)


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str, workspace: str | None = None) -> dict:
    ws = load(workspace) if workspace else current()
    try:
        return session_store.load(ws, session_id).to_dict()
    except FileNotFoundError:
        raise HTTPException(404, f"会话不存在：{session_id}") from None


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, workspace: str | None = None) -> dict:
    ws = load(workspace) if workspace else current()
    session_store.delete(ws, session_id)
    return {"ok": True, "sessions": session_store.list_sessions(ws)}


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #

@app.get("/api/models")
def list_models() -> dict:
    return models.public_state()


@app.post("/api/models")
def add_model(body: ModelIn) -> dict:
    profile = models.add(body.name, body.base_url, body.model, body.api_key, body.thinking)
    return {"ok": True, "id": profile.id, "state": models.public_state()}


@app.post("/api/models/activate")
def activate_model(body: IdIn) -> dict:
    try:
        models.activate(body.id)
    except KeyError:
        raise HTTPException(404, f"模型不存在：{body.id}") from None
    return {"ok": True, "state": models.public_state()}


@app.delete("/api/models/{profile_id}")
def delete_model(profile_id: str) -> dict:
    models.remove(profile_id)
    return {"ok": True, "state": models.public_state()}


# --------------------------------------------------------------------------- #
# 其他
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health() -> dict:
    try:
        ws = current()
        state = models.public_state()
        active = state.get("active")
        label = next((item["name"] for item in state["items"] if item["id"] == active), None)
        if label is None and state.get("fallback"):
            label = f".env:{state['fallback']['model']}"
        return {"ok": True, "workspace": ws.id, "db": ws.db.exists(), "model": label}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
app.mount("/artifacts", StaticFiles(directory=ARTIFACTS_DIR), name="artifacts")
