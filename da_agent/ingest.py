"""把 9 个 CSV 变成 data/olist.db。

流程六步：
    1. 重置      删掉旧库（保证每次跑出来的结果一样）
    2. 逐表读取  pd.read_csv(dtype=str)：全部按字符串读，不让 pandas 猜类型
    3. 按 schema 转换类型，同时统计"转换失败数"
    4. 写入      to_sql(chunksize=10000)
    5. 建索引    主键用 UNIQUE INDEX（pandas 建表时不会带主键）
    6. 校验      行数比对、空值/唯一值统计，写 catalog，打报告

两个关键设计：
· 先全字符串读、再按 schema 转：所有类型转换必须在我们的控制下发生。
  让 pandas 自动推断，order_id 这类"看起来像数字"的列会被转成 int，前导零丢失。
· 转换失败要计数：errors="coerce" 会把坏值静默变 NULL，
  不计数你永远不知道有多少行被悄悄丢了。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table as RichTable

from .config import CATALOG_PATH, CSV_ENCODING, DATA_DIR, DB_PATH, ensure_dirs
from .schema import DERIVED_TABLES, RELATIONSHIPS, TABLES, Table, sql_type

console = Console()


@dataclass
class TableStat:
    name: str
    rows_csv: int = 0
    rows_db: int = 0
    seconds: float = 0.0
    failed: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 类型转换
# --------------------------------------------------------------------------- #

def cast_column(s: pd.Series, kind: str) -> tuple[pd.Series, int]:
    """把一列字符串转成目标类型，返回 (转换后序列, 转换失败数)。"""
    if kind == "text":
        out = s.str.strip()
        return out.mask(out.eq("")), 0

    if kind in ("int", "float"):
        num = pd.to_numeric(s, errors="coerce")
        failed = int((num.isna() & s.notna()).sum())
        return (num.round().astype("Int64") if kind == "int" else num.astype("float64")), failed

    if kind == "datetime":
        dt = pd.to_datetime(s, errors="coerce", format="mixed")
        failed = int((dt.isna() & s.notna()).sum())
        return dt.dt.strftime("%Y-%m-%d %H:%M:%S"), failed

    raise ValueError(f"未知类型: {kind}")


def to_sql_ready(frame: pd.DataFrame) -> pd.DataFrame:
    """转成 object + None。

    原因：NULL 必须是 Python 的 None，pandas 的 pd.NA / NaN 交给 sqlite3 会报
    "Error binding parameter"。这一步看起来多余，但它是 to_sql 稳定的关键。
    """
    obj = frame.astype(object)
    obj[frame.isna()] = None
    return obj


# --------------------------------------------------------------------------- #
# 建库
# --------------------------------------------------------------------------- #

def reset_db(path: Path) -> None:
    """删掉旧库及其附属文件（-wal / -shm / -journal）。"""
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


def write_table(conn: sqlite3.Connection, table: Table) -> TableStat:
    stat = TableStat(name=table.name)
    t0 = time.perf_counter()

    path = DATA_DIR / table.file
    raw = pd.read_csv(path, dtype=str, encoding=CSV_ENCODING, engine="pyarrow")
    stat.rows_csv = len(raw)

    missing = [c.name for c in table.columns if c.name not in raw.columns]
    if missing:
        raise ValueError(f"{table.name}: CSV 里缺少列 {missing}")

    casted: dict[str, pd.Series] = {}
    for col in table.columns:
        series, failed = cast_column(raw[col.name], col.kind)
        casted[col.name] = series
        if failed:
            stat.failed[col.name] = failed
        if not col.nullable and series.isna().any():
            stat.warnings.append(f"{col.name} 声明非空，但有 {int(series.isna().sum())} 个空值")

    frame = pd.DataFrame({c.name: casted[c.name] for c in table.columns})

    to_sql_ready(frame).to_sql(
        table.name,
        conn,
        if_exists="replace",
        index=False,
        chunksize=10_000,
        dtype={c.name: sql_type(c.kind) for c in table.columns},
    )

    stat.rows_db = conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0]
    stat.seconds = time.perf_counter() - t0
    return stat


def create_indexes(conn: sqlite3.Connection, table: Table, stat: TableStat) -> None:
    """主键 + 普通索引。

    SQLite 里 PRIMARY KEY 只能在建表时声明，pandas 的 to_sql 不会带，
    所以这里用 UNIQUE INDEX 达到同样的约束效果。
    """
    if table.pk:
        cols = ", ".join(table.pk)
        dups = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT {cols} FROM {table.name} "
            f"GROUP BY {cols} HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        suffix = "_".join(table.pk)
        if dups:
            stat.warnings.append(f"主键 ({cols}) 有 {dups} 组重复，降级为普通索引")
            conn.execute(f"CREATE INDEX idx_{table.name}_pk ON {table.name}({cols})")
        else:
            conn.execute(f"CREATE UNIQUE INDEX uq_{table.name}_{suffix} ON {table.name}({cols})")

    for col in table.indexes:
        conn.execute(f"CREATE INDEX idx_{table.name}_{col} ON {table.name}({col})")


def build_derived(conn: sqlite3.Connection) -> list[TableStat]:
    stats: list[TableStat] = []
    for d in DERIVED_TABLES:
        t0 = time.perf_counter()
        conn.execute(f"DROP TABLE IF EXISTS {d.name}")
        # 显式建表（保留类型声明），再灌数据
        ddl = ", ".join(f"{name} {ctype}" for name, ctype in d.columns)
        conn.execute(f"CREATE TABLE {d.name} ({ddl})")
        cols = ", ".join(name for name, _ in d.columns)
        conn.execute(f"INSERT INTO {d.name} ({cols}) {d.select_sql}")
        for col in d.indexes:
            conn.execute(f"CREATE INDEX idx_{d.name}_{col} ON {d.name}({col})")
        rows = conn.execute(f"SELECT COUNT(*) FROM {d.name}").fetchone()[0]
        stats.append(TableStat(name=d.name, rows_csv=rows, rows_db=rows,
                               seconds=time.perf_counter() - t0))
    conn.commit()
    return stats


# --------------------------------------------------------------------------- #
# 数据字典（catalog）：从建好的库里读，而不是从 schema 里抄
# --------------------------------------------------------------------------- #

def build_catalog(conn: sqlite3.Connection) -> dict:
    notes = {t.name: t.note for t in TABLES}
    col_notes = {(t.name, c.name): c.note for t in TABLES for c in t.columns}
    notes.update({d.name: d.note for d in DERIVED_TABLES})

    tables = []
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for name in names:
        rows = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        info = conn.execute(f"PRAGMA table_info({name})").fetchall()  # cid,name,type,notnull,dflt,pk
        columns = []
        for _, col, ctype, notnull, _, _pk in info:
            nulls = conn.execute(f"SELECT COUNT(*) FROM {name} WHERE {col} IS NULL").fetchone()[0]
            distinct = conn.execute(f"SELECT COUNT(DISTINCT {col}) FROM {name}").fetchone()[0]
            columns.append({
                "name": col,
                "type": ctype,
                "notnull": bool(notnull),
                "null_pct": round(nulls / rows * 100, 2) if rows else 0.0,
                "distinct": distinct,
                "note": col_notes.get((name, col), ""),
            })
        indexes = [{"name": r[1], "unique": bool(r[2])}
                   for r in conn.execute(f"PRAGMA index_list({name})").fetchall()]
        tables.append({"name": name, "rows": rows, "note": notes.get(name, ""),
                       "columns": columns, "indexes": indexes})

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(DB_PATH),
        "tables": tables,
        "relationships": [
            {"from_table": a, "from_column": b, "to_table": c, "to_column": d}
            for a, b, c, d in RELATIONSHIPS
        ],
    }


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def print_report(stats: list[TableStat], catalog: dict, total_seconds: float) -> None:
    t = RichTable(title="建库报告", header_style="bold cyan", title_justify="left")
    for col in ("表", "CSV 行数", "库内行数", "转换失败", "耗时(s)", "警告"):
        t.add_column(col, overflow="fold")
    for s in stats:
        failed = sum(s.failed.values())
        warn = "；".join(s.warnings) or ""
        t.add_row(s.name, f"{s.rows_csv:,}", f"{s.rows_db:,}",
                  str(failed) if failed else "-", f"{s.seconds:.1f}", warn)
    console.print(t)

    total_rows = sum(s.rows_db for s in stats)
    db_mb = DB_PATH.stat().st_size / 1024 / 1024
    console.print(f"\n共 {len(stats)} 张表 {total_rows:,} 行，库文件 {db_mb:.1f} MB，"
                  f"总耗时 {total_seconds:.1f}s")
    console.print(f"[dim]库: {DB_PATH}[/dim]")
    console.print(f"[dim]数据字典: {CATALOG_PATH}[/dim]")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def build(db_path: Path = DB_PATH, catalog_path: Path = CATALOG_PATH) -> dict:
    ensure_dirs()
    t0 = time.perf_counter()

    reset_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        stats = [write_table(conn, table) for table in TABLES]
        for table, stat in zip(TABLES, stats):
            create_indexes(conn, table, stat)
        conn.commit()
        stats.extend(build_derived(conn))

        catalog = build_catalog(conn)
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        conn.close()

    print_report(stats, catalog, time.perf_counter() - t0)
    return catalog


def main() -> None:
    build()


if __name__ == "__main__":
    main()
