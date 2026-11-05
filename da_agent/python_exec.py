"""在独立子进程里跑 Python（pandas / matplotlib）。

为什么用子进程而不是 exec()：
1. **超时能真的杀掉**。exec 里的死循环没法中断，只能等它自己结束；
2. 崩了不会带走服务进程；
3. 每次都是干净环境，上一次的变量不会污染下一次。

代价是状态不保留——每次要给的数据都得重新 query。换来的是确定性和一条清晰的安全边界。

**这不是沙箱**：模型写的代码就在你的账户下跑，能读能写能联网。
它防的是"手滑"和"忘了关的循环"，不是恶意代码。要真隔离得上容器。

产物收集：跑完把输出目录里的图片/CSV 收上来，前端直接展示。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import ARTIFACTS_DIR

MAX_STDOUT = 4_000
MAX_STDERR = 2_000
COLLECT_SUFFIXES = {".png", ".jpg", ".svg", ".csv", ".html", ".json"}

PRELUDE = '''\
import atexit
import os
import warnings

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

DB_PATH = os.environ.get("DA_DB_PATH", "")
OUT_DIR = os.environ.get("DA_OUT_DIR", ".")
_URI = "file:" + DB_PATH.replace("\\\\", "/") + "?mode=ro"

# 中文字体：Windows 上不加这句，标题全是豆腐块
plt.rcParams["font.sans-serif"] = [
    "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC", "DejaVu Sans",
]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.figsize"] = (8, 4.5)


def query(sql):
    """执行一条只读 SQL，返回 DataFrame。"""
    import sqlite3

    conn = sqlite3.connect(_URI, uri=True)
    try:
        return pd.read_sql_query(sql, conn)
    finally:
        conn.close()


def _save_figures():
    """脚本结束时把所有还开着的图存下来——用户不需要写 plt.show()。"""
    for index in plt.get_fignums():
        try:
            figure = plt.figure(index)
            figure.savefig(os.path.join(OUT_DIR, f"fig-{index}.png"),
                           dpi=140, bbox_inches="tight")
        except Exception:
            pass


atexit.register(_save_figures)
'''


def _collect(run_dir: Path) -> list[dict]:
    items = []
    for path in sorted(run_dir.iterdir()):
        if path.suffix.lower() not in COLLECT_SUFFIXES or path.name == "main.py":
            continue
        relative = path.relative_to(ARTIFACTS_DIR).as_posix()
        items.append({
            "name": path.name,
            "path": str(path.relative_to(ARTIFACTS_DIR.parent)).replace("\\", "/"),
            "url": f"/artifacts/{relative}",
            "kind": "image" if path.suffix.lower() in {".png", ".jpg", ".svg"} else "file",
        })
    return items


def execute(code: str, db_path: Path, workspace_id: str = "default",
            timeout: int = 60) -> dict:
    """跑一段代码，返回 {ok, stdout, error, artifacts, elapsed_ms}。"""
    timeout = max(5, min(int(timeout), 300))

    run_dir = ARTIFACTS_DIR / workspace_id / "runs" / f"{datetime.now():%Y%m%d-%H%M%S-%f}"
    run_dir.mkdir(parents=True, exist_ok=True)

    script = run_dir / "main.py"
    script.write_text(PRELUDE + "\n\n" + code + "\n", encoding="utf-8", newline="\n")

    env = os.environ.copy()
    env.update({
        "DA_DB_PATH": str(Path(db_path).resolve()),
        "DA_OUT_DIR": str(run_dir),
        "PYTHONIOENCODING": "utf-8",
        "MPLBACKEND": "Agg",
    })

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(run_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        ok = proc.returncode == 0
        stdout = (proc.stdout or "")[-MAX_STDOUT:]
        error = None if ok else ((proc.stderr or "").strip()[-MAX_STDERR:] or "执行失败")
    except subprocess.TimeoutExpired:
        ok = False
        stdout = ""
        error = f"执行超时（超过 {timeout} 秒），已终止"
    except Exception as exc:                      # 起不来进程之类
        ok = False
        stdout = ""
        error = f"{type(exc).__name__}: {exc}"

    elapsed = round((time.perf_counter() - started) * 1000)
    return {
        "ok": ok,
        "stdout": stdout,
        "error": error,
        "artifacts": _collect(run_dir),
        "elapsed_ms": elapsed,
    }
