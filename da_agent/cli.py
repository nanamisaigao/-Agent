"""命令行入口。

命令分组：
    profile / ingest   数据准备（CSV → SQLite + 数据字典）
    ws ...             工作区管理（一个数据库 = 一个工作区）
    sql / ask          查询与提问
    serve / config     Web 服务与配置自检
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table as RichTable

from .config import CATALOG_PATH, DATA_DIR, DB_PATH
from .inspect import profile_all, show, to_json

app = typer.Typer(add_completion=False, no_args_is_help=True, help="数据分析 agent 的命令行入口")
ws_app = typer.Typer(add_completion=False, no_args_is_help=True,
                      help="工作区：一个数据库 = 一个工作区")
app.add_typer(ws_app, name="ws")

console = Console()


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #

@app.command()
def version() -> None:
    """打印版本号。"""
    from . import __version__

    console.print(f"da_agent {__version__}")


@app.command()
def config() -> None:
    """检查当前模型配置与工作区（不会显示密钥内容）。"""
    import os

    from . import models
    from .config import PROJECT_ROOT
    from .workspace import current

    settings = models.current_settings()
    profile = models.active_profile()
    env_file = PROJECT_ROOT / ".env"
    thinking = settings.thinking or (
        "disabled（deepseek 自动默认）" if "deepseek" in settings.base_url else "未设置")

    source = f"模型档案 {profile.name}（{profile.id}）" if profile else ".env 环境变量"
    console.print(f"模型来源 : {source}")
    console.print(f"base_url : {settings.base_url}")
    console.print(f"model    : {settings.model}")
    if settings.api_key:
        console.print(f"api_key  : 已设置（{settings.api_key[:3]}…，共 {len(settings.api_key)} 字符）")
    else:
        console.print("api_key  : [red]未设置[/red]")
    console.print(f"thinking : {thinking}")
    console.print(f".env     : {'已加载' if env_file.exists() else '不存在'}")

    try:
        ws = current()
        console.print(f"工作区   : [green]{ws.id}[/green]（{ws.name}，{ws.tables} 张表 / {ws.rows:,} 行）")
        console.print(f"数据库   : {ws.db}")
    except Exception as exc:
        console.print(f"工作区   : [red]{exc}[/red]")


# --------------------------------------------------------------------------- #
# 数据准备
# --------------------------------------------------------------------------- #

@app.command()
def profile(
    table: str | None = typer.Option(None, "--table", "-t", help="只看这一张表（表名片段）"),
    detail: bool = typer.Option(False, "--detail", "-d", help="打印每张表的列明细"),
    json_out: Path | None = typer.Option(None, "--json", help="把完整画像写到这个 JSON 文件"),
    data_dir: Path = typer.Option(DATA_DIR, "--data-dir", help="CSV 所在目录"),
) -> None:
    """扫描 CSV，输出每张表的行数、列、空值率、取值范围。"""
    profiles = profile_all(data_dir)
    show(profiles, detail=detail, only=table)

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(to_json(profiles), encoding="utf-8")
        console.print(f"\n[dim]完整画像已写入 {json_out}[/dim]")


@app.command()
def ingest(
    db: Path = typer.Option(DB_PATH, "--db", help="输出数据库路径"),
    catalog: Path = typer.Option(CATALOG_PATH, "--catalog", help="输出数据字典路径"),
    register: bool = typer.Option(True, "--register/--no-register",
                                  help="建完后同步到工作区 olist"),
) -> None:
    """把 data/ 下的 CSV 建成 SQLite 库（每次运行都会重建）。"""
    from .ingest import build

    built = build(db, catalog)

    if register and db.resolve() == DB_PATH.resolve():
        from .workspace import create

        create(DB_PATH, name="Olist 电商", catalog=built, ws_id="olist")
        console.print("[dim]已同步到工作区 olist（业务笔记 notes.md 不会被覆盖）[/dim]")


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #

@ws_app.command("list")
def ws_list() -> None:
    """列出所有工作区，● 标记当前激活的那个。"""
    from .workspace import active_id, list_all, load

    items = list_all()
    if not items:
        console.print("还没有工作区。用 `ws add <数据库路径>` 添加一个。")
        return

    active = active_id()
    table = RichTable(header_style="bold cyan")
    for column in ("", "ID", "名称", "表", "行数", "数据库"):
        table.add_column(column, justify="right" if column in ("表", "行数") else "left")

    for meta in items:
        workspace = load(meta["id"])
        mark = "[green]●[/green]" if workspace.id == active else ""
        table.add_row(mark, workspace.id, workspace.name,
                      str(workspace.tables), f"{workspace.rows:,}", str(workspace.db))
    console.print(table)


@ws_app.command("add")
def ws_add(
    path: Path = typer.Argument(..., help="SQLite 数据库文件路径"),
    name: str = typer.Option(None, "--name", help="显示名，默认用文件名"),
    notes: Path = typer.Option(None, "--notes", help="业务笔记 markdown 文件（可选）"),
    use: bool = typer.Option(True, "--use/--no-use", help="添加后立即切换过去"),
) -> None:
    """注册一个新数据库：自动分析结构并存到本地。"""
    from .workspace import create, set_active

    text = notes.read_text(encoding="utf-8") if notes else ""
    console.print("[dim]正在分析数据库结构…[/dim]")
    workspace = create(path, name=name, notes=text)
    if use:
        set_active(workspace.id)

    console.print(f"[green]已完成[/green] {workspace.name}（{workspace.id}）："
                  f"{workspace.tables} 张表，{workspace.rows:,} 行")
    console.print(f"[dim]数据字典 {workspace.dir / 'catalog.json'}[/dim]")
    console.print(f"[dim]业务笔记 {workspace.dir / 'notes.md'}（可手工编辑）[/dim]")


@ws_app.command("use")
def ws_use(ws_id: str = typer.Argument(..., help="工作区 ID")) -> None:
    """切换当前工作区。"""
    from .workspace import load, set_active

    workspace = load(ws_id)
    set_active(workspace.id)
    console.print(f"当前工作区：[green]{workspace.name}[/green]（{workspace.id}）")


@ws_app.command("show")
def ws_show(ws_id: str = typer.Argument(None, help="工作区 ID，默认当前")) -> None:
    """查看某个工作区的数据字典和业务笔记。"""
    from .workspace import current, load

    workspace = load(ws_id) if ws_id else current()
    console.print(f"[bold]{workspace.name}[/bold]  [dim]({workspace.id})[/dim]")
    console.print(f"[dim]{workspace.db}[/dim]\n")

    table = RichTable(header_style="bold cyan")
    table.add_column("表")
    table.add_column("行数", justify="right")
    table.add_column("列", justify="right")
    table.add_column("备注")
    for entry in workspace.catalog.get("tables", []):
        table.add_row(entry["name"], f"{int(entry.get('rows', 0)):,}",
                      str(len(entry.get("columns", []))), entry.get("note", ""))
    console.print(table)

    if workspace.notes:
        console.print(Panel(workspace.notes, title="业务笔记 notes.md", border_style="dim"))


@ws_app.command("refresh")
def ws_refresh(ws_id: str = typer.Argument(None, help="工作区 ID，默认当前")) -> None:
    """重新分析数据库结构（数据变了之后用）。notes.md 不会被覆盖。"""
    from .workspace import create, current, load

    workspace = load(ws_id) if ws_id else current()
    console.print("[dim]重新分析中…[/dim]")
    refreshed = create(workspace.db, name=workspace.name, ws_id=workspace.id)
    console.print(f"[green]已刷新[/green] {refreshed.name}：{refreshed.tables} 张表，"
                  f"{refreshed.rows:,} 行")


# --------------------------------------------------------------------------- #
# 模型档案
# --------------------------------------------------------------------------- #

model_app = typer.Typer(add_completion=False, no_args_is_help=True,
                        help="模型档案：保存多套配置，随时切换")
app.add_typer(model_app, name="model")


@model_app.command("list")
def model_list() -> None:
    """列出已保存的模型配置。"""
    from . import models

    state = models.public_state()
    if not state["items"]:
        console.print("还没有保存模型。可以在网页里添加，或用 `model add`。")
        if state.get("fallback"):
            console.print(f"[dim]当前回落到 .env：{state['fallback']['model']} "
                          f"@ {state['fallback']['base_url']}[/dim]")
        return

    table = RichTable(header_style="bold cyan")
    for column in ("", "ID", "名称", "模型", "端点", "密钥"):
        table.add_column(column)
    for item in state["items"]:
        mark = "[green]●[/green]" if item["id"] == state["active"] else ""
        table.add_row(mark, item["id"], item["name"], item["model"],
                      item["base_url"], item["key_hint"])
    console.print(table)


@model_app.command("add")
def model_add(
    name: str = typer.Option(..., "--name", help="显示名，例如 DeepSeek"),
    key: str = typer.Option(..., "--key", help="API key"),
    base_url: str = typer.Option("https://api.deepseek.com", "--base-url"),
    model: str = typer.Option("deepseek-flash", "--model"),
    thinking: str = typer.Option(None, "--thinking", help="enabled / disabled"),
) -> None:
    """保存一套模型配置并切换过去。"""
    from . import models

    profile = models.add(name, base_url, model, key, thinking)
    console.print(f"[green]已保存[/green] {profile.name}（{profile.id}），并设为当前模型")


@model_app.command("use")
def model_use(profile_id: str = typer.Argument(..., help="模型 ID")) -> None:
    """切换当前模型。"""
    from . import models

    models.activate(profile_id)
    console.print(f"当前模型：[green]{profile_id}[/green]")


# --------------------------------------------------------------------------- #
# 查询与提问
# --------------------------------------------------------------------------- #

@app.command()
def sql(
    query: str = typer.Argument(..., help="只读 SQL 查询（单条 SELECT / WITH）"),
    limit: int = typer.Option(50, "--limit", "-n", help="最多返回多少行"),
    timeout: float = typer.Option(30.0, "--timeout", help="超时秒数"),
    as_json: bool = typer.Option(False, "--json", help="按 JSON 输出（和前端/模型看到的一致）"),
    ws: str | None = typer.Option(None, "--ws", help="工作区 ID，默认当前"),
) -> None:
    """在只读连接上执行一条 SELECT，看到 agent 将来看到的那份结果。"""
    from .db import run_query, summary_line
    from .workspace import current, load

    workspace = load(ws) if ws else current()
    result = run_query(query, limit=limit, timeout=timeout, path=workspace.db)

    if as_json:
        console.print_json(json.dumps(asdict(result), ensure_ascii=False, default=str))
        return

    if not result.ok:
        console.print(f"[red]SQL 执行失败[/red] {result.error}")
        if result.hint:
            console.print(f"[yellow]提示[/yellow] {result.hint}")
        raise typer.Exit(code=1)

    table = RichTable(header_style="bold cyan", title_justify="left")
    for name, kind in zip(result.columns, result.types):
        table.add_column(f"{name}  [dim]{kind}[/dim]", overflow="fold")
    for row in result.rows:
        table.add_row(*["NULL" if v is None else str(v) for v in row])
    console.print(table)
    console.print(f"[dim]{summary_line(result)}[/dim]")


def _render(event: dict) -> None:
    """把事件渲染成人能读的样子。

    Web 前端渲染的是同一批事件——所以这里只做"展示"，不做任何业务判断。
    """
    kind = event.get("type")

    if kind == "text":
        console.print(event["text"], style="dim")

    elif kind == "tool_call":
        args = event.get("args")
        sql_text = args.get("sql") if isinstance(args, dict) else args
        console.print(f"[cyan]→ {event['name']}[/cyan] [dim]step {event.get('step')}[/dim]")
        if sql_text:
            console.print(Panel(str(sql_text).strip(), border_style="cyan", padding=(0, 1)))

    elif kind == "tool_result":
        result = event.get("result") or {}
        if not result.get("ok"):
            console.print(f"[red]✗ {result.get('error')}[/red]")
            if result.get("hint"):
                console.print(f"[yellow]  {result['hint']}[/yellow]")
            return
        table = RichTable(show_header=True, header_style="bold", title_justify="left")
        for name in result.get("columns") or []:
            table.add_column(str(name), overflow="fold")
        for row in result.get("rows") or []:
            table.add_row(*["NULL" if value is None else str(value) for value in row])
        if result.get("columns"):
            console.print(table)
        bits = [f"{result.get('returned', 0)} 行", f"{result.get('elapsed_ms', 0):.0f} ms"]
        if result.get("truncated"):
            bits.append("已截断")
        console.print(f"[dim]{' · '.join(bits)}[/dim]")
        if result.get("hint"):
            console.print(f"[yellow]{result['hint']}[/yellow]")

    elif kind == "answer":
        console.print()
        console.print(Markdown(event.get("text") or ""))

    elif kind == "error":
        console.print(f"[red]错误：{event.get('message')}[/red]")

    elif kind == "run_end":
        usage = event.get("usage") or {}
        ok = "完成" if event.get("ok") else "未完成"
        console.print(
            f"[dim]{ok} · {event.get('steps')} 步 · "
            f"prompt {usage.get('prompt_tokens', 0)} / "
            f"completion {usage.get('completion_tokens', 0)} tokens[/dim]"
        )


@app.command()
def ask(
    question: str = typer.Argument(..., help="你的问题"),
    max_steps: int = typer.Option(8, "--max-steps", help="最多几轮工具调用"),
    fake: bool = typer.Option(False, "--fake", help="离线演示：不调模型，用脚本客户端验证循环"),
    trace: bool = typer.Option(True, "--trace/--no-trace", help="把事件流写成 JSONL 便于复盘"),
    ws: str | None = typer.Option(None, "--ws", help="工作区 ID，默认当前"),
) -> None:
    """问一个问题，看 agent 一步步怎么查出来。"""
    from . import models
    from .llm import EchoClient
    from .loop import ApiClient, run
    from .trace import write_trace
    from .workspace import current, load

    workspace = load(ws) if ws else current()
    console.rule(f"[bold]{question}[/bold] [dim]· {workspace.name}[/dim]")
    client = EchoClient() if fake else ApiClient(models.current_settings())

    records: list[dict] = []
    for event in run(question, client=client, max_steps=max_steps, workspace=workspace):
        records.append(event)
        _render(event)

    if trace:
        path = write_trace(records)
        if path:
            console.print(f"\n[dim]事件流已保存：{path}[/dim]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="绑定地址"),
    port: int = typer.Option(8123, "--port", help="端口"),
) -> None:
    """启动 Web 界面。"""
    import uvicorn

    console.print(f"[bold]打开 http://{host}:{port}[/bold]  （Ctrl+C 停止）")
    uvicorn.run("da_agent.server:app", host=host, port=port, log_level="warning")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
