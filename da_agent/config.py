"""全局路径配置：项目里任何"文件在哪"的问题，只在这里回答。

为什么单独一个文件：
1. 后面 agent 要读写文件时，路径不散落在各处，改一次就全改；
2. PyCharm 里点运行按钮时，工作目录可能不是项目根，靠 __file__ 定位才可靠。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .env import load_dotenv

# __file__ = <项目根>/da_agent/config.py，parents[1] 就是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = PROJECT_ROOT / "data"          # 原始 CSV（只读，不要改）
DB_PATH = DATA_DIR / "olist.db"           # 由 CSV 生成的 SQLite 库（衍生品，可重建）
CATALOG_PATH = DATA_DIR / "olist.catalog.json"  # 数据字典（衍生品）
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"  # 图、导出结果、报告（后面才用）
TRACES_DIR = ARTIFACTS_DIR / "traces"       # 每次问答的事件流与完整上下文（JSONL）

# 工作区：一个数据库 = 一个目录（元数据 + 数据字典 + 笔记）
WORKSPACES_DIR = PROJECT_ROOT / "workspaces"
ACTIVE_WS_FILE = WORKSPACES_DIR / "active.json"

# 所有表共用的读取参数：Olist 的 CSV 带 BOM 和引号，这一组参数能通吃
CSV_ENCODING = "utf-8-sig"


@dataclass
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    timeout: float


def llm_settings() -> LLMSettings:
    """每次调用时现读环境变量。

    不在 import 时读死：这样 .env 在之后加载也能生效，测试也好改。
    变量名优先用项目自己的 DA_*，其次兼容 OpenAI 的标准名——
    这样你在别处已经配好的 OPENAI_API_KEY 可以直接复用。
    """
    load_dotenv(PROJECT_ROOT / ".env")
    return LLMSettings(
        base_url=(os.getenv("DA_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL")
                  or "https://api.openai.com/v1"),
        api_key=(os.getenv("DA_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""),
        model=(os.getenv("DA_LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"),
        timeout=float(os.getenv("DA_LLM_TIMEOUT", "120")),
    )


def ensure_dirs() -> None:
    """创建运行时需要的目录（已存在则不动）。"""
    for d in (DATA_DIR, ARTIFACTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
