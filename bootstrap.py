#!/usr/bin/env python3
"""一键安装依赖并启动。

用法：
    python bootstrap.py              安装依赖并启动网页
    python bootstrap.py --no-serve   只装依赖，不启动
    python bootstrap.py --port 9000  指定端口
    python bootstrap.py --reinstall  强制重装依赖

设计原则：**能重复跑**。已经装好的东西就跳过，所以第二次开始基本是秒开。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
# 依赖指纹放在 artifacts 下（已 gitignore、可写），不放 .venv 里——
# 有些环境会对 venv 目录做额外保护，写失败也没必要让启动挂掉
DEPS_MARK = ROOT / "artifacts" / ".da_deps"
MIN_VERSION = (3, 10)

DEFAULT_PORT = 8123
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def line(text: str = "") -> None:
    print(text, flush=True)


def step(index: int, total: int, text: str) -> None:
    line(f"[{index}/{total}] {text}")


def fail(text: str, hint: str = "") -> None:
    line()
    line(f"  ✗ {text}")
    if hint:
        for row in hint.splitlines():
            line(f"    {row}")
    line()


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #

def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def requirements_hash() -> str:
    content = (ROOT / "requirements.txt").read_bytes()
    return hashlib.sha256(content).hexdigest()[:16]


def deps_ready() -> bool:
    if not venv_python().exists() or not DEPS_MARK.exists():
        return False
    try:
        return DEPS_MARK.read_text(encoding="utf-8").strip() == requirements_hash()
    except OSError:
        return False


def mark_deps() -> None:
    try:
        DEPS_MARK.parent.mkdir(parents=True, exist_ok=True)
        DEPS_MARK.write_text(requirements_hash(), encoding="utf-8", newline="\n")
    except OSError:
        pass          # 写不进去就下次重装一遍，不影响使用


def free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="安装依赖并启动 da_agent")
    parser.add_argument("--no-serve", action="store_true", help="只安装，不启动")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="网页端口")
    parser.add_argument("--reinstall", action="store_true", help="强制重装依赖")
    args = parser.parse_args()

    line("=" * 58)
    line("  da_agent · 一键安装")
    line("=" * 58)
    line()

    # 1. 版本
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < MIN_VERSION:
        fail(f"Python 版本太低：{version}",
             "需要 3.10 或更高。\n下载：https://www.python.org/downloads/")
        return 1
    line(f"  Python {version}（{sys.executable}）")
    line(f"  项目目录 {ROOT}")
    line()

    total = 3 if args.no_serve else 4

    # 2. 虚拟环境
    step(1, total, "准备虚拟环境 .venv …")
    if venv_python().exists():
        line("      已存在，跳过")
    else:
        result = subprocess.run([sys.executable, "-m", "venv", str(VENV)])
        if result.returncode != 0 or not venv_python().exists():
            fail("创建虚拟环境失败",
                 "Debian/Ubuntu 上可能需要先装：sudo apt install python3-venv")
            return 1
        line("      已创建")

    # 3. 依赖
    step(2, total, "检查依赖 …")
    python = str(venv_python())
    if deps_ready() and not args.reinstall:
        line("      requirements.txt 没变，跳过安装")
    else:
        line("      正在安装（第一次会比较慢，几分钟正常）…")
        base = [python, "-m", "pip", "install", "--disable-pip-version-check",
                "-r", "requirements.txt"]
        result = subprocess.run(base)
        if result.returncode != 0:
            line()
            line("      官方源失败，改用清华镜像重试 …")
            result = subprocess.run(base + ["-i", MIRROR])
        if result.returncode != 0:
            fail("依赖安装失败",
                 "常见原因：网络不通、Python 版本太旧、缺少编译工具。\n"
                 "可以手动重试：.venv 里的 python -m pip install -r requirements.txt")
            return 1
        mark_deps()
        line("      完成")

    # 4. 数据与配置提示
    step(3, total, "检查数据和模型配置 …")
    workspaces = list((ROOT / "workspaces").glob("*/workspace.json"))
    env_file = ROOT / ".env"
    models_file = ROOT / "config" / "models.json"
    has_model = env_file.exists() or models_file.exists()

    line(f"      工作区：{len(workspaces)} 个" + ("（没有？先跑 ingest 或 ws add）" if not workspaces else ""))
    line(f"      模型：{'已配置' if has_model else '未配置（启动后在网页左下角添加即可）'}")
    line()

    if args.no_serve:
        line("  ✓ 安装完成。启动命令：python -m da_agent.cli serve")
        line()
        return 0

    # 5. 启动
    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    step(4, total, f"启动服务 {url}")
    line("      Ctrl+C 停止")
    line()

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # 服务器/无桌面环境可以设 DA_NO_BROWSER=1 跳过自动开浏览器
    if os.getenv("DA_NO_BROWSER") != "1":
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    try:
        code = subprocess.call([python, "-m", "da_agent.cli", "serve", "--port", str(port)],
                               cwd=str(ROOT), env=env)
    except KeyboardInterrupt:
        code = 0
    line()
    line("  已停止")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
