"""工作区：一个数据库 = 一个目录。

    workspaces/
      olist/
        workspace.json   # 元数据：名字、数据库路径、创建时间
        catalog.json     # 数据字典：表、列、类型、行数、空值率、自动生成的特征
        notes.md         # 人写的业务知识（口径、陷阱），可选

为什么这么分：
· catalog 是**自动生成**的，可以随时重建；
· notes 是**人写**的，重建不会覆盖；
· 两者分开，才不会出现"重新分析一次，手写的经验全没了"。

这也解决了原来的问题：agent 之前把 Olist 的业务规则焊死在 context.py 里，
换个库就没法用。现在那些规则属于"这个工作区的笔记"，而不是"agent 的代码"。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import ACTIVE_WS_FILE, PROJECT_ROOT, WORKSPACES_DIR
from .db import connect_ro

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")

# Olist 工作区的业务知识。注意：它属于"数据"，不属于"代码"。
OLIST_NOTES = """\
- `orders.customer_id` 是"每单一个号"，不是真实客户。用户维度必须用 `customers.customer_unique_id`。
- `order_items` 一行 = 一个商品行，不是一单。订单数用 `COUNT(DISTINCT order_id)` 或直接查 `orders`。
- 地址请用 `geolocation_zip`（每邮编一行）。直接 join 原始 `geolocation` 会让 10 万行变成 1500 万行。
- `order_delivered_customer_date` 有 3% 空值（取消/未送达），算履约时长要 `WHERE ... IS NOT NULL`。
- `products` 有 73 个分类，翻译表只有 71 条。分类汇总要 LEFT JOIN 并保留未翻译的。
- `order_payments` 一单可能有多笔（组合支付），按单汇总要 SUM。
- 可靠分析窗口：2017-01-01 ~ 2018-08-31（2016 年仅 300 余单，2018-09 后只有 20 单）。
"""


@dataclass
class Workspace:
    id: str
    name: str
    db: Path
    dir: Path
    catalog: dict
    notes: str = ""

    @property
    def tables(self) -> int:
        return len(self.catalog.get("tables", []))

    @property
    def rows(self) -> int:
        return sum(int(t.get("rows", 0)) for t in self.catalog.get("tables", []))


# --------------------------------------------------------------------------- #
# 路径与小工具
# --------------------------------------------------------------------------- #

def _quote(name: str) -> str:
    """给表名/列名加双引号。有的库表名带空格或中文，不加引号会语法错误。"""
    return '"' + str(name).replace('"', '""') + '"'


def _one(conn, sql: str):
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


def slugify(name: str) -> str:
    """把名字变成安全的目录名。"""
    slug = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fa5]+", "-", name.strip()).strip("-").lower()
    return slug or "workspace"


def suggest_questions(catalog: dict, limit: int = 3) -> list[str]:
    """从数据字典里推几个入手问题。

    为什么要自动生成：界面上的示例问题如果写死，换个库就变成笑话
    （在销售库里提示"哪个州的订单最多"）。这里只用结构信息，不调模型，
    所以免费、离线可用、加库时不依赖 API key。

    挑选顺序看重"能不能问出东西"：有时间列和低基数列的表更有分析价值，
    纯坐标表（如 geolocation）排在后面。
    """
    def score(table: dict) -> tuple:
        columns = table.get("columns", [])
        has_time = any(normalize_type(c.get("type")) == "datetime" for c in columns)
        has_category = any(normalize_type(c.get("type")) == "text"
                           and 1 < (c.get("distinct") or 0) <= 40 for c in columns)
        return (has_time, has_category, int(table.get("rows") or 0))

    tables = [t for t in catalog.get("tables", []) if int(t.get("rows") or 0) > 0]
    tables.sort(key=score, reverse=True)

    questions: list[str] = []
    for table in tables:
        columns = table.get("columns", [])
        name = table["name"]

        time_col = next((c["name"] for c in columns
                         if normalize_type(c.get("type")) == "datetime"), None)
        category = next((c["name"] for c in columns
                         if normalize_type(c.get("type")) == "text"
                         and 1 < (c.get("distinct") or 0) <= 40), None)
        numeric = next((c["name"] for c in columns
                        if normalize_type(c.get("type")) in ("int", "float")
                        and not str(c["name"]).lower().endswith("id")
                        and (c.get("distinct") or 0) > 10), None)

        if category:
            questions.append(f"{name} 里各 {category} 分别有多少条？")
        elif time_col:
            questions.append(f"{name} 按月的数据量怎么变化？")
        else:
            questions.append(f"{name} 一共有多少行数据？")

        if numeric and len(questions) < limit:
            questions.append(f"{name} 的 {numeric} 平均值是多少？")
        if time_col and len(questions) < limit:
            questions.append(f"{name} 的时间范围是从什么时候到什么时候？")

        if len(questions) >= limit:
            break

    return questions[:limit]


def _rel(path: Path) -> str:
    """项目内的路径存相对路径，方便整个目录搬走。"""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(Path(path).resolve())


def _abs(stored: str) -> Path:
    path = Path(stored)
    return path if path.is_absolute() else PROJECT_ROOT / path


# --------------------------------------------------------------------------- #
# 画像：把任意一个 SQLite 库变成数据字典
# --------------------------------------------------------------------------- #

def normalize_type(value: str) -> str:
    """把各种写法归一成四种：text / int / float / datetime。

    SQLite 的声明类型五花八门（TEXT、INTEGER、VARCHAR(20)、REAL…），
    早期建库脚本写的是大写声明类型，后来画像写的是小写语义类型，
    两边不一致会让"按类型挑问题"这类逻辑失效。
    """
    text = str(value or "").strip().lower()
    if "int" in text:
        return "int"
    if any(key in text for key in ("real", "floa", "doub", "num", "dec")):
        return "float"
    if "date" in text or "time" in text:
        return "datetime"
    return "text"


def infer_kind(declared: str, samples: list) -> str:
    """在声明类型的基础上，用样本值再补一刀（TEXT 里可能存的是 ISO 时间）。"""
    kind = normalize_type(declared)
    values = [s for s in samples if s is not None]
    if kind == "text" and values and all(_ISO.match(str(s)) for s in values):
        return "datetime"
    return kind


def profile_db(db_path: Path, distinct_limit: int = 200_000) -> dict:
    """扫描一个 SQLite 库，生成数据字典。

    distinct 统计在大表上很贵（全表扫描），所以超过 distinct_limit 行就跳过，
    并在 note 里写明"未统计"——让 agent 知道"这里没有精确基数"，
    而不是让它误以为基数是 0。
    """
    conn = connect_ro(db_path)
    try:
        tables = []
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]

        for name in names:
            rows = int(_one(conn, f"SELECT COUNT(*) FROM {_quote(name)}") or 0)
            columns = []
            pk_candidates: list[str] = []
            high_null: list[str] = []

            for _cid, col, ctype, _notnull, _dflt, _pk in conn.execute(
                f"PRAGMA table_info({_quote(name)})"
            ):
                nulls = int(_one(
                    conn,
                    f"SELECT COUNT(*) FROM {_quote(name)} WHERE {_quote(col)} IS NULL",
                ) or 0)
                null_pct = round(nulls / rows * 100, 2) if rows else 0.0

                distinct = None
                if rows and rows <= distinct_limit:
                    distinct = int(_one(
                        conn,
                        f"SELECT COUNT(DISTINCT {_quote(col)}) FROM {_quote(name)}",
                    ) or 0)

                samples = [
                    row[0]
                    for row in conn.execute(
                        f"SELECT {_quote(col)} FROM {_quote(name)} "
                        f"WHERE {_quote(col)} IS NOT NULL LIMIT 3"
                    )
                ]
                kind = infer_kind(ctype, samples)

                note = ""
                if distinct is not None and rows and distinct == rows and nulls == 0 and rows > 1:
                    note = "值唯一"
                    pk_candidates.append(col)
                elif distinct is None:
                    note = "基数未统计（表较大）"
                if null_pct >= 20:
                    note = (note + "；" if note else "") + "缺失较多"
                    high_null.append(f"{col}({null_pct:.0f}%)")

                columns.append({
                    "name": col,
                    "type": kind,
                    "declared": ctype or "",
                    "notnull": bool(_notnull),
                    "null_pct": null_pct,
                    "distinct": distinct,
                    "note": note,
                })

            table_note = [f"{rows:,} 行"]
            if pk_candidates and len(pk_candidates) <= 3:
                table_note.append("疑似主键 " + "、".join(pk_candidates))
            if high_null:
                table_note.append("高缺失 " + "、".join(high_null[:4]))

            tables.append({
                "name": name,
                "rows": rows,
                "note": "；".join(table_note),
                "columns": columns,
                "indexes": [
                    {"name": r[1], "unique": bool(r[2])}
                    for r in conn.execute(f"PRAGMA index_list({_quote(name)})")
                ],
            })
    finally:
        conn.close()

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(db_path),
        "tables": tables,
        "relationships": [],
    }


# --------------------------------------------------------------------------- #
# 增删查
# --------------------------------------------------------------------------- #

def create(db_path: Path, name: str | None = None, notes: str = "",
           catalog: dict | None = None, ws_id: str | None = None) -> Workspace:
    db_path = Path(db_path).resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")

    name = name or db_path.stem
    ws_id = ws_id or slugify(name)
    folder = WORKSPACES_DIR / ws_id
    folder.mkdir(parents=True, exist_ok=True)

    catalog = catalog or profile_db(db_path)
    # 入手问题跟着字典一起存下来，界面上直接展示
    catalog.setdefault("suggestions", suggest_questions(catalog))
    (folder / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    # 已存在的 notes.md 不覆盖——人写的东西比自动生成的值钱
    notes_path = folder / "notes.md"
    if not notes_path.exists():
        notes_path.write_text(notes, encoding="utf-8", newline="\n")

    meta = {
        "id": ws_id,
        "name": name,
        "db": _rel(db_path),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (folder / "workspace.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    return load(ws_id)


def load(ws_id: str) -> Workspace:
    folder = WORKSPACES_DIR / ws_id
    meta_path = folder / "workspace.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"工作区不存在：{ws_id}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    catalog_path = folder / "catalog.json"
    catalog = (
        json.loads(catalog_path.read_text(encoding="utf-8"))
        if catalog_path.exists() else {"tables": []}
    )
    # 老字典可能是大写声明类型，读的时候统一一次
    for table in catalog.get("tables", []):
        for column in table.get("columns", []):
            column["type"] = normalize_type(column.get("type", "text"))
    notes_path = folder / "notes.md"
    notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""

    return Workspace(
        id=meta["id"],
        name=meta.get("name", meta["id"]),
        db=_abs(meta["db"]),
        dir=folder,
        catalog=catalog,
        notes=notes.strip(),
    )


def list_all() -> list[dict]:
    """只读元数据，不加载 catalog——列表要快。"""
    if not WORKSPACES_DIR.exists():
        return []
    items = []
    for folder in sorted(WORKSPACES_DIR.iterdir()):
        meta_path = folder / "workspace.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        items.append(meta)
    return items


def active_id() -> str | None:
    if not ACTIVE_WS_FILE.exists():
        return None
    try:
        return json.loads(ACTIVE_WS_FILE.read_text(encoding="utf-8")).get("id")
    except (json.JSONDecodeError, OSError):
        return None


def remove(ws_id: str) -> None:
    """删掉工作区目录（数据字典、业务笔记、会话）。

    **不会删你的 .db 文件**——那可能是唯一一份原始数据，删了就找不回来。
    """
    folder = WORKSPACES_DIR / ws_id
    if not (folder / "workspace.json").exists():
        raise FileNotFoundError(f"工作区不存在：{ws_id}")

    shutil.rmtree(folder, ignore_errors=True)

    if active_id() == ws_id:
        remaining = list_all()
        if remaining:
            set_active(remaining[0]["id"])
        elif ACTIVE_WS_FILE.exists():
            ACTIVE_WS_FILE.unlink()


def set_active(ws_id: str) -> None:
    load(ws_id)                                  # 不存在就直接报错，别写坏 active
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_WS_FILE.write_text(
        json.dumps({"id": ws_id}, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def current() -> Workspace:
    """当前工作区。第一次用时自动把老的 Olist 库登记进来。"""
    ws_id = active_id()
    if ws_id and (WORKSPACES_DIR / ws_id / "workspace.json").exists():
        return load(ws_id)

    items = list_all()
    if items:
        set_active(items[0]["id"])
        return load(items[0]["id"])

    return ensure_default()


def ensure_default() -> Workspace:
    """把 data/olist.db 登记成第一个工作区。

    如果 ingest 已经生成过 catalog，直接复用——省一次全表扫描，
    也保住了当时算出来的精确基数。
    """
    from .config import CATALOG_PATH, DB_PATH

    if not DB_PATH.exists():
        raise FileNotFoundError(
            "还没有任何工作区，也没有 data/olist.db。\n"
            "先跑 `python -m da_agent.cli ingest` 建库，"
            "或者 `python -m da_agent.cli ws add <你的.db>` 注册一个。"
        )

    catalog = None
    if CATALOG_PATH.exists():
        try:
            catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            catalog = None

    ws = create(DB_PATH, name="Olist 电商", notes=OLIST_NOTES, catalog=catalog, ws_id="olist")
    set_active(ws.id)
    return ws
