"""事件流落盘。

每次问答写一个 JSONL，一行一个事件。用途：
1. 复盘——出问题时能看到模型到底看到了什么；
2. 评测——批量回放这些事件，比对结果；
3. 前端"历史会话"的数据源（下一步）。

一行一个 JSON 的好处是可以直接 `Get-Content xxx.jsonl | Select -Last 1` 看尾部，
不用解析整个文件。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import TRACES_DIR


def write_trace(events, directory: Path = TRACES_DIR) -> Path | None:
    records = list(events)
    if not records:
        return None

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now():%Y%m%d-%H%M%S-%f}.jsonl"
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False, default=str) for event in records),
        encoding="utf-8",
        newline="\n",
    )
    return path
