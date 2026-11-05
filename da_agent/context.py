"""上下文构建：把数据字典压成模型能读的"表卡片"。

为什么不给模型一个 describe_table 工具让它自己探：
1. 表只有 10 张、列不到 70 个，一次性写进系统提示约 1k token，很划算；
2. 探测要花 2~3 轮工具调用，每轮都是一次完整请求，反而更贵更慢；
3. 固定内容放在最前面，最容易命中 prompt cache。

原则：**能预先知道的，就不要让模型现场问。**
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import CATALOG_PATH

RULES = """## 工作规则

1. 只通过 run_sql 取数。所有数字必须来自查询结果，禁止估算、禁止编造。
2. 一次只验证一个假设：先取最小必要的数据，看到结果再决定下一步。
3. 时间字段是文本 `YYYY-MM-DD HH:MM:SS`；按月分组用 `strftime('%Y-%m', 列名)`。
4. 金额是 REAL 类型；汇总后用 `ROUND(x, 2)`。
5. 可靠分析窗口：2017-01-01 ~ 2018-08-31（2016 年仅 300 余单，2018-09 后只有 20 单）。
6. 问题有口径歧义时，**先说明你的假设再执行**，不要反问用户——你没有追问的机会。
7. 最终回答用中文，结构为：结论 → 关键数字 → 最后一行写明口径。
"""

PITFALLS = """## 已知陷阱（踩过，别重踩）

- `orders.customer_id` 是"每单一个号"，不是真实客户。用户维度必须用 `customers.customer_unique_id`。
- `order_items` 一行 = 一个商品行，不是一单。订单数用 `COUNT(DISTINCT order_id)` 或直接查 `orders`。
- 地址请用 `geolocation_zip`（每邮编一行）。直接 join 原始 `geolocation` 会让 10 万行变成 1500 万行。
- `order_delivered_customer_date` 有 3% 空值（取消/未送达），算履约时长要 `WHERE ... IS NOT NULL`。
- `products` 有 73 个分类，翻译表只有 71 条。分类汇总要 LEFT JOIN 并保留未翻译的。
- `order_payments` 一单可能有多笔（组合支付），按单汇总要 SUM。
"""


def build_schema_card(catalog_path: Path = CATALOG_PATH) -> str:
    if not catalog_path.exists():
        return "（数据字典不存在：请先运行 `python -m da_agent.cli ingest`）"

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    lines = [f"## 数据库结构（SQLite，只读，共 {len(catalog['tables'])} 张表）", ""]
    for table in catalog["tables"]:
        columns = ", ".join(f"{c['name']} {c['type']}" for c in table["columns"])
        lines.append(f"### {table['name']}（{table['rows']:,} 行）")
        lines.append(f"列：{columns}")
        if table.get("note"):
            lines.append(f"备注：{table['note']}")
        lines.append("")
    return "\n".join(lines)


def build_system_prompt(catalog_path: Path = CATALOG_PATH) -> str:
    return "\n".join([
        "你是一个数据分析助手，通过查询一个 Olist 巴西电商数据库来回答中文问题。",
        "",
        build_schema_card(catalog_path),
        RULES,
        PITFALLS,
        "现在开始。先用 run_sql 确认数据，再下结论。",
    ])
