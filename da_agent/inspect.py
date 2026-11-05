"""看数据：把 CSV 的"形状"和"内容"变成人能读的画像。

这一步只读，不写库。画像的用途有三个：
1. 你确认自己下载的数据到底长什么样（列名、空值、取值范围）；
2. 推断每列应该用什么类型（全部当 TEXT 是后面 SQL 出错的头号原因）；
3. 之后原样喂给 agent，作为它的"schema 记忆"。

注意：这里推断类型只是候选值，最终类型由 schema.py 人工确认后固化。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

from .config import CSV_ENCODING, DATA_DIR

Console_ = Console(width=150, highlight=False)

# 类型推断用的正则：先看内容长得像什么，而不是相信列名
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")

SAMPLE_ROWS = 5000  # 推断类型时只看前 5000 个非空值，够用且快


@dataclass
class ColumnProfile:
    name: str
    kind: str            # date_time / date / int / float / text
    null_pct: float      # 空值占比 %
    distinct: int        # 唯一值个数
    span: str            # 取值范围（数值/日期才有）
    sample: str          # 第一个非空样例


@dataclass
class TableProfile:
    table: str           # 表名（文件名去掉 .csv 和 olist_ 前缀）
    file: str
    rows: int
    columns: list[ColumnProfile] = field(default_factory=list)

    @property
    def pk_candidates(self) -> list[str]:
        """既是 100% 非空、唯一值又等于行数的列，很可能是主键。"""
        return [c.name for c in self.columns if c.null_pct == 0 and c.distinct == self.rows and self.rows > 0]


def read_csv(path: Path) -> pd.DataFrame:
    """统一用字符串读：先看清原文，再决定类型。"""
    try:
        return pd.read_csv(path, dtype=str, encoding=CSV_ENCODING, engine="pyarrow")
    except Exception:
        return pd.read_csv(path, dtype=str, encoding=CSV_ENCODING)


def infer_kind(values: pd.Series) -> str:
    s = values.dropna().astype(str)
    if s.empty:
        return "text"
    sample = s.head(SAMPLE_ROWS)
    n = len(sample)

    def frac(pattern: re.Pattern[str]) -> float:
        return sum(1 for v in sample if pattern.match(v)) / n

    if frac(_TIMESTAMP) > 0.95:
        return "date_time"
    if frac(_DATE) > 0.95:
        return "date"
    if frac(_INT) > 0.95:
        return "int"
    if frac(_FLOAT) > 0.95:
        return "float"
    return "text"


def _span(series: pd.Series, kind: str) -> str:
    s = series.dropna()
    if s.empty:
        return ""
    if kind in ("int", "float"):
        v = pd.to_numeric(s, errors="coerce").dropna()
        if v.empty:
            return ""
        return f"{v.min():g} ~ {v.max():g}"
    if kind in ("date", "date_time"):
        return f"{s.min()[:10]} ~ {s.max()[:10]}"
    return ""


def profile_table(path: Path) -> TableProfile:
    df = read_csv(path)
    table = path.stem.replace("olist_", "").replace("_dataset", "")

    cols: list[ColumnProfile] = []
    for name in df.columns:
        s = df[name]
        kind = infer_kind(s)
        first = s.dropna()
        cols.append(
            ColumnProfile(
                name=name,
                kind=kind,
                null_pct=round(float(s.isna().mean() * 100), 2),
                distinct=int(s.nunique(dropna=True)),
                span=_span(s, kind),
                sample=str(first.iloc[0])[:38] if not first.empty else "",
            )
        )
    return TableProfile(table=table, file=path.name, rows=len(df), columns=cols)


def profile_all(data_dir: Path = DATA_DIR) -> list[TableProfile]:
    files = sorted(data_dir.glob("*.csv"))
    if not files:
        raise SystemExit(f"在 {data_dir} 下没找到 CSV，检查一下数据位置。")
    return [profile_table(p) for p in files]


def to_json(profiles: list[TableProfile]) -> str:
    return json.dumps([asdict(p) for p in profiles], ensure_ascii=False, indent=2)


def render_summary(profiles: list[TableProfile]) -> Table:
    t = Table(title="表一览", header_style="bold cyan", title_justify="left")
    for col in ("表名", "行数", "列数", "候选主键", "文件"):
        t.add_column(col, overflow="fold")
    for p in profiles:
        pks = ", ".join(p.pk_candidates) or "-"
        t.add_row(p.table, f"{p.rows:,}", str(len(p.columns)), pks, p.file)
    return t


def render_detail(p: TableProfile) -> Table:
    t = Table(title=f"{p.table}  ({p.rows:,} 行)", header_style="bold green", title_justify="left")
    for col, width in (("列名", None), ("类型", 10), ("空值%", 7), ("唯一值", 9), ("范围", 26), ("样例", None)):
        t.add_column(col, width=width, overflow="fold")
    for c in p.columns:
        t.add_row(c.name, c.kind, f"{c.null_pct:.1f}", f"{c.distinct:,}", c.span, c.sample)
    return t


def show(profiles: list[TableProfile], detail: bool = False, only: str | None = None) -> None:
    selected = [p for p in profiles if only is None or only in p.table]
    Console_.print(render_summary(selected))
    if detail:
        for p in selected:
            Console_.print()
            Console_.print(render_detail(p))
