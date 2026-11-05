"""run_sql 工具：唯一的数据获取通道。

返回给模型的是结构化 JSON（列名、行、是否截断、耗时、错误、提示），
不是 CLI 里那张漂亮的表——模型只吃文本，而且表越大越要压缩。
"""

from __future__ import annotations

from ..db import run_query
from .base import Tool, ToolContext


def _run(args: dict, ctx: ToolContext) -> dict:
    sql = str(args.get("sql") or "").strip()
    limit = max(1, min(int(args.get("limit") or 30), 200))

    result = run_query(sql, limit=limit, path=ctx.db_path)
    payload = {
        "ok": result.ok,
        "columns": result.columns,
        "types": result.types,
        "rows": [list(row) for row in result.rows],
        "returned": result.returned,
        "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms,
    }

    if not result.ok:
        payload["error"] = result.error
        payload["hint"] = result.hint
    elif result.returned == 0:
        # 空结果是最常见的"看起来没错其实错了"，主动提醒一句
        payload["hint"] = "查询成功但 0 行：检查过滤条件是否过严，或该组合确实没有数据"
    elif result.truncated:
        payload["hint"] = f"只返回了前 {limit} 行，需要完整结果请加聚合或提高 limit"

    return payload


RUN_SQL = Tool(
    name="run_sql",
    description=(
        "在只读的 SQLite 数据库上执行一条 SELECT 查询，用来获取分析所需的原始数据。\n"
        "规则：\n"
        "- 只支持单条 SELECT / WITH 语句；多条语句、写操作、PRAGMA 都会被拒绝。\n"
        "- 表结构见系统提示中的表卡片；时间列多为文本 'YYYY-MM-DD HH:MM:SS'，"
        "按月分组用 strftime('%Y-%m', 列名)。\n"
        "- 金额是 REAL 类型，做汇总后请用 ROUND(x, 2)。\n"
        "- 结果默认最多 30 行；要拿全量请用聚合（GROUP BY）而不是 SELECT *。\n"
        "- 报错时返回值里会有 hint，按提示修改 SQL 再试一次。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "一条完整的 SELECT 或 WITH 查询，结尾不要加分号。",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少行，默认 30，上限 200。",
                "default": 30,
            },
        },
        "required": ["sql"],
    },
    fn=_run,
)
