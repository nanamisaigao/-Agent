"""上下文构建：把数据字典压成模型能读的"表卡片"。

为什么把表结构直接写进系统提示，而不是给模型一个 describe_table 工具：
1. 一次写进去约 1k token，比让它探 2~3 轮（每轮都是一次完整请求）便宜得多；
2. 固定内容放在最前面，最容易命中 prompt cache；
3. 模型第一轮就知道有哪些表、哪些列有坑，不会瞎猜。

原则：**能预先知道的，就不要让模型现场问。**

注意这里已经与具体数据库解耦：入参是 catalog（数据字典），不是某个硬编码的库。
Olist 的业务知识挪到了工作区的 notes.md 里——那是"数据"，不是"代码"。
"""

from __future__ import annotations

MAX_TABLES = 40        # 表特别多时截断，避免系统提示失控
MAX_COLUMNS = 40

RULES = """## 工作规则

1. 取数只通过 run_sql，或在 run_python 里用 query(sql)。所有数字必须来自查询结果，禁止估算、禁止编造。
2. 一次只验证一个假设：先取最小必要的数据，看到结果再决定下一步。
3. **先确认表的粒度**（一行代表什么）再 join。join 后行数明显变多，说明粒度对不上，
   要去重或先聚合成一张"每个实体一行"的表再连。
4. 时间列多数存成文本（`YYYY-MM-DD HH:MM:SS`）。先确认格式，再用
   `strftime('%Y-%m', 列名)` 分组；SQLite 没有 DATE_TRUNC。
5. 金额/小数汇总后用 `ROUND(x, 2)`。
6. 问题有口径歧义时，**先说明你的假设再执行**，不要反问用户——你没有追问的机会。
7. 最终回答用中文：结论 → 关键数字 → 最后一行写明口径。
"""

TOOLS = """## 两个工具怎么分工

- **run_sql**：取数、过滤、join、聚合。能用 SQL 表达的优先用它，省 token 也更快。
- **run_python**：SQL 不擅长的事——透视表、时间序列重采样、统计、**以及画图**。
  环境里有 pd / np / plt，用 `query(sql)` 拿 DataFrame；画图不用 `plt.show()`，
  脚本结束会自动保存并把图展示给用户。

**涉及到"画个图""趋势看看""分布怎么样"时，主动用 run_python 出图**，
不要只丢一张表格给用户。图比表直观，这是这个工具存在的意义。
"""


def build_schema_card(catalog: dict) -> str:
    tables = catalog.get("tables", [])
    if not tables:
        return "## 数据库结构\n（数据字典为空：这个工作区还没有被分析过）"

    lines = [f"## 数据库结构（SQLite，只读，共 {len(tables)} 张表）", ""]

    for table in tables[:MAX_TABLES]:
        columns = table.get("columns", [])
        rendered = [
            f"{column['name']} {column.get('type', 'text')}"
            for column in columns[:MAX_COLUMNS]
        ]
        if len(columns) > MAX_COLUMNS:
            rendered.append(f"…还有 {len(columns) - MAX_COLUMNS} 列")

        lines.append(f"### {table['name']}（{int(table.get('rows', 0)):,} 行）")
        lines.append("列：" + ", ".join(rendered))
        if table.get("note"):
            lines.append(f"备注：{table['note']}")
        lines.append("")

    if len(tables) > MAX_TABLES:
        lines.append(f"…还有 {len(tables) - MAX_TABLES} 张表未列出")

    return "\n".join(lines)


def build_system_prompt(catalog: dict, notes: str = "", workspace_name: str = "") -> str:
    parts = [
        "你是一个数据分析助手，通过 SQL 查询一个只读的 SQLite 数据库来回答中文问题。",
    ]
    if workspace_name:
        parts.append(f"当前数据集：{workspace_name}")

    parts.append("")
    parts.append(build_schema_card(catalog))
    parts.append(RULES)
    parts.append(TOOLS)

    if notes:
        parts.append("## 这个数据集已知的情况（人工整理，优先采信）")
        parts.append(notes)
        parts.append("")

    parts.append("现在开始。需要看图的时候就用 run_python 画出来。")
    return "\n".join(parts)
