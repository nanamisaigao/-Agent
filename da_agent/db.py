"""只读数据库访问层：agent 的"眼睛"。

三条设计原则：
1. 物理只读：用 `mode=ro` 打开。就算前面的检查全被绕过，SQLite 也会拒绝写。
2. 语法围栏：只允许单条 SELECT / WITH。多语句、PRAGMA、ATTACH 一律拒。
3. 超时可控：progress handler 到点中断，不让一个笛卡尔积把进程卡死。

围栏和只读是两层防线：**围栏负责友好报错，只读负责真的改不动。**
只做前者是纸糊的安全，只做后者会让 agent 拿到一句看不懂的 "attempt to write a readonly database"。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import DB_PATH

MAX_SQL_CHARS = 8_000

# 只认 SELECT / WITH 开头。注意 WITH 后面也可能是 SELECT，SQLite 不支持"改数据的 CTE"
_SQL_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

# 用来先把字符串字面量掏空，再判断有没有分号 / 关键字
_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")

_VALUE_TYPES = {int: "int", float: "float", str: "text", bytes: "blob"}


@dataclass
class QueryResult:
    """统一结果信封。CLI、Web 前端、agent 上下文都吃这一份结构。"""

    ok: bool
    sql: str
    columns: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False          # 是否还有更多行没取
    elapsed_ms: float = 0.0
    error: str | None = None
    hint: str | None = None          # 给模型和人看的可操作提示

    @property
    def returned(self) -> int:
        return len(self.rows)


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #

def connect_ro(path: Path) -> sqlite3.Connection:
    """只读连接。路径要用正斜杠，Windows 的反斜杠在 file: URI 里会被当成转义。

    路径是必填的：早期版本默认指向 data/olist.db，多工作区之后那个默认值
    只会变成"忘了传参就用错库"的陷阱。
    """
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


# --------------------------------------------------------------------------- #
# 围栏
# --------------------------------------------------------------------------- #

def check_sql(sql: str) -> str | None:
    """通过返回 None，不通过返回错误原因。"""
    text = sql.strip()
    if not text:
        return "SQL 为空"
    if len(text) > MAX_SQL_CHARS:
        return f"SQL 太长（超过 {MAX_SQL_CHARS} 字符）"

    bare = _LITERAL.sub("''", text)          # 掏空字面量，避免 'a;b' 被误判
    bare = bare.rstrip().rstrip(";").rstrip()
    if ";" in bare:
        return "只允许一条语句，检测到多个分号"

    if not _SQL_START.match(bare):
        return "只允许 SELECT 或 WITH 开头的查询；需要改数据请走 ingest 重建流程"
    return None


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #

def _column_type(rows: list[tuple], index: int) -> str:
    """按第一个非空值推断列类型。SQLite 是动态类型，只能这样看。"""
    for row in rows:
        value = row[index]
        if value is not None:
            return _VALUE_TYPES.get(type(value), type(value).__name__)
    return "null"


def _hint(conn: sqlite3.Connection | None, error: str) -> str:
    """把 SQLite 的错误翻译成下一步该干什么。这是 agent 能不能自我修复的关键。"""
    e = error.lower()
    if "no such table" in e and conn is not None:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return "可用表：" + ", ".join(names)
    if "no such column" in e:
        return "列名拼写？用 PRAGMA table_info(表名) 核对；注意 product_name_lenght 是源数据的拼写错误"
    if "syntax error" in e:
        return "SQLite 没有 DATE_TRUNC / QUALIFY；按月分组用 strftime('%Y-%m', 列名)"
    if "interrupted" in e:
        return "查询超时：先加 WHERE 缩小范围，或分步聚合"
    if "readonly" in e:
        return "数据库以只读方式打开，只能查询"
    return ""


def run_query(sql: str, *, path: Path, limit: int = 50,
              timeout: float = 30.0) -> QueryResult:
    """执行一条只读查询。

    path 是关键字必填参数：多工作区下没有"默认库"这回事。
    """
    result = QueryResult(ok=False, sql=sql)

    refused = check_sql(sql)
    if refused:
        result.error = refused
        result.hint = _hint(None, refused)
        return result

    t0 = time.perf_counter()
    conn = connect_ro(path)
    try:
        deadline = t0 + timeout
        # 每 10000 条虚拟机指令回调一次，超时就返回非 0 让 SQLite 中断
        conn.set_progress_handler(
            lambda: 1 if time.perf_counter() > deadline else 0, 10_000)

        cur = conn.execute(sql)
        # 多取一行用来判断"是否还有更多"，比再跑一次 COUNT(*) 便宜得多
        rows = cur.fetchmany(limit + 1)
        result.truncated = len(rows) > limit
        rows = rows[:limit]

        result.columns = [d[0] for d in (cur.description or [])]
        result.rows = [tuple(r) for r in rows]
        result.types = [_column_type(rows, i) for i in range(len(result.columns))]
        result.ok = True
    except sqlite3.Error as exc:
        result.error = str(exc)
        result.hint = _hint(conn, result.error)
    finally:
        conn.close()

    result.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    return result


# --------------------------------------------------------------------------- #
# 给模型看的摘要
# --------------------------------------------------------------------------- #

def preview_md(result: QueryResult, max_rows: int = 20, max_width: int = 40) -> str:
    """把结果渲染成 markdown 表格。

    为什么要限制行数和列宽：模型的上下文是有限资源。
    一张 1 万行的表塞进去，后面的推理就没钱花了。
    """
    if not result.ok or not result.columns:
        return ""

    def cell(value: object) -> str:
        text = "NULL" if value is None else str(value)
        text = text.replace("|", "\\|").replace("\n", " ")
        return text if len(text) <= max_width else text[: max_width - 1] + "…"

    header = "| " + " | ".join(result.columns) + " |"
    sep = "| " + " | ".join("---" for _ in result.columns) + " |"
    body = ["| " + " | ".join(cell(v) for v in row) + " |" for row in result.rows[:max_rows]]
    return "\n".join([header, sep, *body])


def summary_line(result: QueryResult) -> str:
    if not result.ok:
        return f"失败：{result.error}"
    suffix = "（还有更多行未返回）" if result.truncated else ""
    return f"{result.returned} 行 · {result.elapsed_ms:.0f} ms{suffix}"
