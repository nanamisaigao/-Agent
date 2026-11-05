"""命令行入口。

目前只有一条命令 profile（看数据）。
后面每加一个能力，这里就多一条命令，命令名尽量和 agent 的工具名一致：
    profile  → 数据画像        run_sql   → 跑 SQL
    ingest   → 建库            ask       → 提问
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
console = Console()


@app.command()
def version() -> None:
    """打印版本号。"""
    from . import __version__

    console.print(f"da_agent {__version__}")


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
) -> None:
    """把 data/ 下的 CSV 建成 SQLite 库（每次运行都会重建）。"""
    from .ingest import build  # 延迟导入：pandas 很重，别拖慢 version 这种命令

    build(db, catalog)


@app.command()
def sql(
    query: str = typer.Argument(..., help="只读 SQL 查询（单条 SELECT / WITH）"),
    limit: int = typer.Option(50, "--limit", "-n", help="最多返回多少行"),
    timeout: float = typer.Option(30.0, "--timeout", help="超时秒数"),
    as_json: bool = typer.Option(False, "--json", help="按 JSON 输出（和前端/模型看到的一致）"),
) -> None:
    """在只读连接上执行一条 SELECT，看到 agent 将来看到的那份结果。"""
    from .db import run_query, summary_line

    result = run_query(query, limit=limit, timeout=timeout)

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

    Web 前端将来渲染的是同一批事件——所以这里只做"展示"，不做任何业务判断。
    """
    kind = event.get("type")

    if kind == "text":
        console.print(event["text"], style="dim")

    elif kind == "tool_call":
        args = event.get("args")
        sql = args.get("sql") if isinstance(args, dict) else args
        console.print(f"[cyan]→ {event['name']}[/cyan] [dim]step {event.get('step')}[/dim]")
        if sql:
            console.print(Panel(str(sql).strip(), border_style="cyan", padding=(0, 1)))

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
) -> None:
    """问一个问题，看 agent 一步步怎么查出来。"""
    from datetime import datetime

    from .config import TRACES_DIR
    from .llm import EchoClient
    from .loop import ApiClient, run

    console.rule(f"[bold]{question}[/bold]")
    client = EchoClient() if fake else ApiClient()

    records: list[str] = []
    for event in run(question, client=client, max_steps=max_steps):
        records.append(json.dumps(event, ensure_ascii=False, default=str))
        _render(event)

    if trace:
        TRACES_DIR.mkdir(parents=True, exist_ok=True)
        path = TRACES_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        path.write_text("\n".join(records), encoding="utf-8")
        console.print(f"\n[dim]事件流已保存：{path}[/dim]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
