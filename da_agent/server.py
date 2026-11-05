"""Web 服务：把 loop 的事件流用 SSE 推给浏览器。

三个设计要点：

1. **复用同一个 loop.run()，业务逻辑一行不重写。**
   这就是"一个内核，多个前端"：CLI 是渲染器，浏览器也是渲染器。

2. **SSE 包一个同步生成器。**
   我们的模型调用是阻塞式 urllib，放进 async def 会卡死事件循环。
   Starlette 对同步生成器会自动放到线程池里跑，正好合用。

3. **每个请求是一次独立会话，且绑定一个工作区。**
   工作区决定"查哪个库、用哪份数据字典"。请求不带 workspace 就用当前激活的。
"""

from __future__ import annotations

import json
from typing import Iterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ARTIFACTS_DIR, PROJECT_ROOT
from .llm import EchoClient
from .loop import ApiClient, run
from .trace import write_trace
from .workspace import current, list_all, load

WEB_DIR = PROJECT_ROOT / "web"

app = FastAPI(title="da_agent", docs_url="/api/docs")


class AskRequest(BaseModel):
    question: str
    max_steps: int = 8
    fake: bool = False          # 离线演示：不调模型，验证前端渲染用
    workspace: str | None = None


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


@app.post("/api/ask")
def ask(req: AskRequest) -> StreamingResponse:
    def stream() -> Iterator[str]:
        records: list[dict] = []
        client = EchoClient() if req.fake else ApiClient()
        try:
            workspace = load(req.workspace) if req.workspace else current()
            for event in run(req.question, client=client,
                             max_steps=req.max_steps, workspace=workspace):
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
            "X-Accel-Buffering": "no",     # 防止反向代理缓冲，否则"流式"就变成一次性
        },
    )


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


@app.get("/api/tables")
def tables(workspace: str | None = None) -> list[dict]:
    """当前工作区的表清单，给左侧栏用。"""
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


@app.get("/api/health")
def health() -> dict:
    try:
        ws = current()
        return {"ok": True, "workspace": ws.id, "db": ws.db.exists()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
app.mount("/artifacts", StaticFiles(directory=ARTIFACTS_DIR), name="artifacts")
