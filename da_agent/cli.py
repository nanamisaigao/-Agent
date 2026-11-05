"""命令行入口。

目前只有一条命令 profile（看数据）。
后面每加一个能力，这里就多一条命令，命令名尽量和 agent 的工具名一致：
    profile  → 数据画像        run_sql   → 跑 SQL
    ingest   → 建库            ask       → 提问
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from .config import DATA_DIR
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
