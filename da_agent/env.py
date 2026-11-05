"""极简 .env 加载器（约 20 行，替代 python-dotenv）。

为什么要自己写：
1. 加一个依赖只为读几行 KEY=VALUE 不值当；
2. 读一遍源码你就知道 .env 是什么——它只是"进程启动前帮你设置环境变量"的约定，
   没有任何魔法。

规则：已存在的环境变量优先，.env 只补缺（和主流工具的行为一致）。
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> int:
    """把 .env 里的键值补进 os.environ，返回补了几个。"""
    if not path.exists():
        return 0

    added = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            added += 1
    return added
