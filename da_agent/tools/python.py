"""run_python 工具：pandas 处理 + matplotlib 画图。

和 run_sql 的分工：
    run_sql    取数、聚合、join —— 能用 SQL 表达的，别写 Python
    run_python 复杂清洗、透视、统计、作图 —— SQL 不适合的，交给 pandas
分工清楚，token 才省得下来。
"""

from __future__ import annotations

from ..python_exec import execute
from .base import Tool, ToolContext


def _run(args: dict, ctx: ToolContext) -> dict:
    code = str(args.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "code 不能为空"}

    timeout = int(args.get("timeout") or 60)
    result = execute(code, db_path=ctx.db_path, workspace_id=ctx.workspace_id,
                     timeout=timeout)

    payload = {
        "ok": result["ok"],
        "stdout": result["stdout"],
        "elapsed_ms": result["elapsed_ms"],
        "artifacts": result["artifacts"],
        "images": [item["url"] for item in result["artifacts"] if item["kind"] == "image"],
        "files": [item["url"] for item in result["artifacts"] if item["kind"] != "image"],
    }
    if not result["ok"]:
        payload["error"] = result["error"]
        payload["hint"] = "看 traceback 最后一行的错误；数据请用 query(sql) 重新取，变量不会跨调用保留"
    elif not result["artifacts"]:
        payload["hint"] = "执行成功但没有产物：画了图会自动保存，想导出表格用 df.to_csv('x.csv')"

    return payload


RUN_PYTHON = Tool(
    name="run_python",
    description=(
        "在独立的 Python 子进程里执行代码，用来做 SQL 不擅长的处理和**画图**。\n"
        "环境里已经准备好：pandas（pd）、numpy（np）、matplotlib.pyplot（plt，Agg 后端）。\n"
        "取数据用 query(sql)：df = query(\"SELECT ...\")，走只读连接，返回 DataFrame。\n"
        "画图直接 plt.plot(...) 或 df.plot(...)，**不要调 plt.show()**——"
        "脚本结束时所有图会自动保存并展示给用户。\n"
        "中文标题可以直接写，字体已配置；想导出表格就 df.to_csv('结果.csv', index=False)。\n"
        "注意：每次调用都是全新进程，变量不保留，需要的数据要重新 query。\n"
        "什么时候用：需要分组透视、时间序列重采样、统计检验、或者要给用户看图表。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的完整 Python 代码，可以多行。",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认 60，最大 300。",
                "default": 60,
            },
        },
        "required": ["code"],
    },
    fn=_run,
)
