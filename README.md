# da_agent

一个可以接**任意 SQLite 数据库**的数据分析 agent：你用中文提问，它自己写 SQL、查库、看结果、再决定下一步，最后给结论——每一步的 SQL 和查询结果都摊开在你面前。

![界面截图](docs/images/screenshot.png)

---

## 它是什么

- **会自己找数**：模型通过 `run_sql` 工具逐步探索，而不是一次性写出一条巨长的 SQL
- **可以换库**：每个数据库是一个「工作区」，接入时自动分析表结构并存到本地
- **可以换模型**：网页里切换任意 OpenAI 兼容服务（DeepSeek / OpenAI / 本地 Ollama …）
- **只读安全**：查询只走只读连接，只允许单条 `SELECT`，有超时中断
- **过程透明**：每一步的 SQL、返回行数、耗时都能看到，出问题能定位到具体哪一次查询

---

## 快速开始

### 1. 装环境

需要 Python 3.11+（开发时用的是 3.13）。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS / Linux 把第二行换成 `source .venv/bin/activate`。

### 2. 准备数据

项目自带 Olist 巴西电商数据（9 个 CSV）。建库：

```powershell
python -m da_agent.cli ingest
```

约 15 秒，产出 `data/olist.db`（168 MB，10 张表 156 万行）和数据字典。

### 3. 配置模型

复制 `.env.example` 为 `.env`，填上你的 key：

```ini
DA_LLM_BASE_URL=https://api.deepseek.com
DA_LLM_API_KEY=sk-你的密钥
DA_LLM_MODEL=deepseek-flash
DA_LLM_THINKING=disabled
```

自检：

```powershell
python -m da_agent.cli config
```

> `.env` 和 `config/` 都在 `.gitignore` 里，密钥不会进版本库。
> 也可以完全不用 `.env`，直接在网页的「模型设置」里填——界面上保存的配置优先级更高。

### 4. 跑起来

**网页**

```powershell
python -m da_agent.cli serve
# 打开 http://127.0.0.1:8123
```

**命令行**

```powershell
python -m da_agent.cli ask "2017 年哪个州的订单数最多？"
```

---

## 接入你自己的数据库

任何 SQLite 文件都行（`.db` / `.sqlite` / `.sqlite3`）。

**网页**：左侧栏点 `＋`，填文件路径。

**命令行**：

```powershell
python -m da_agent.cli ws add D:\data\mydb.db --name "我的业务库"
```

它会做三件事：

1. 开只读连接扫出所有表和视图
2. 逐列统计类型、行数、空值率、唯一值、样本值
3. 把结果写进 `workspaces/<id>/`，并自动切过去

```
workspaces/
  olist/
    workspace.json   # 元数据（名字、库路径、创建时间）
    catalog.json     # 数据字典（自动生成，可重建）
    notes.md         # 业务知识（人写，不会被覆盖）
```

**`notes.md` 是这个设计里最有用的部分**：自动分析只能看出「`customer_id` 值唯一」，看不出「它其实是每单一个号、不是真客户」。把你踩过的坑写进去，agent 每次都会读到。

---

## 命令速查

| 命令 | 作用 |
|---|---|
| `profile [-d]` | 扫描 CSV，看行数/类型/空值率 |
| `ingest` | CSV → SQLite，并同步工作区 |
| `ws list` | 列出工作区（`●` 是当前） |
| `ws add <路径> [--name]` | 接入新数据库并自动分析 |
| `ws use <id>` | 切换工作区 |
| `ws show [id]` | 看表清单和业务笔记 |
| `ws refresh [id]` | 数据结构变了之后重新分析 |
| `sql "<查询>" [--ws id]` | 手动跑一条只读 SQL |
| `ask "<问题>" [--ws id]` | 提问，看完整过程 |
| `serve [--port]` | 启动网页 |
| `model list / add / use` | 管理模型配置 |
| `config` | 检查模型与工作区状态 |

网页里的调试开关：

```
?demo=1              预置一段对话，不发请求，专门调样式
?demo=1&theme=dark   直接看深色
?fake=1              真发请求但用假模型，不花 token
```

---

## 它是怎么工作的

```
        你提问
          │
          ▼
   ┌──────────────┐   工具调用    ┌──────────────┐
   │  agent loop  │ ───────────▶ │    run_sql   │
   │  (推理)      │ ◀─────────── │  (只读查询)   │
   └──────┬───────┘   结果+摘要    └──────────────┘
          │
          │ 事件流（纯 JSON）
          ▼
    ┌──────────┬───────────┐
    │  CLI     │  Web(SSE)  │
    └──────────┴───────────┘
```

核心是**一个内核，多个前端**：`loop.run()` 只负责推理并吐出事件，CLI 和浏览器各自渲染同一批事件。加一个新界面不需要改任何业务逻辑。

事件类型：`run_start` / `text` / `tool_call` / `tool_result` / `answer` / `run_end` / `error`。

一次问答的上下文长这样（`artifacts/traces/*.jsonl` 里存的就是完整版）：

```
system     表结构 + 工作规则 + 你的 notes.md
user       2017 年哪个州的订单数最多？
assistant  （空文本 + 一个 tool_call）
tool       {"ok": true, "columns": ["customer_state","orders"], "rows": [...]}
assistant  结论……
```

---

## 目录结构

```
da_agent/
  config.py      路径与配置（唯一知道"文件在哪"的地方）
  workspace.py   工作区：目录结构、数据库画像
  schema.py      Olist 的表定义（建库用）
  inspect.py     CSV 画像
  ingest.py      CSV → SQLite
  db.py          只读查询：围栏 / 超时 / 错误提示
  context.py     把数据字典压成系统提示
  llm.py         OpenAI 兼容的 /chat/completions（手写 HTTP）
  loop.py        主循环
  models.py      模型档案（多套配置）
  trace.py       事件流落盘
  server.py      FastAPI + SSE
  cli.py         命令行
  tools/         工具注册表（run_sql）

web/             前端（原生 ES module，无构建步骤）
  index.html
  style.css
  js/app.js      状态、对话流、侧栏、弹窗
  js/markdown.js 纯函数：迷你 markdown 渲染

data/            原始 CSV 与建好的库（gitignored）
workspaces/      每个数据库一份：元数据 + 字典 + 笔记
artifacts/       traces、截图（gitignored）
docs/            方案文档、截图
```

---

## 几个设计选择

**为什么手写 HTTP 而不是用 openai SDK**
协议本质就是「POST 一段 JSON，拿回一段 JSON」。写一遍就知道换服务商该改哪三处，出问题也能直接看到请求和响应原文。

**为什么结果只回摘要**
`SELECT *` 可能返回百万行。工具默认只回 30 行并标注是否截断——模型的上下文是钱，也是注意力。

**为什么把表结构写进系统提示**
给模型一个 `describe_table` 工具让它自己探，要花 2~3 轮请求。表结构固定，一次写进去更便宜也更快，而且稳定前缀能命中 prompt cache（DeepSeek 缓存命中价差 50 倍）。

**为什么 `ToolContext` 要单独传**
工具函数不读全局变量，而是接收「这次查哪个库」。这样同一进程能服务多个工作区，测试也能塞临时库进去。

**为什么 SQL 不折行**
窄屏下折行的 SQL 会变成碎片，完全没法读。宁可横向滚动。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| 每次提问独立 | 不能追问「那 2018 年呢」，需要重新描述上下文 |
| 只支持 SQLite | MySQL/Postgres 需要另写方言适配层 |
| 模型档案明文存 `config/models.json` | 本地工具，靠 `.gitignore` 兜底；不要在共享机器上用 |
| 大表跳过精确基数 | 超过 20 万行不算 `COUNT(DISTINCT)`，字典里标注「未统计」 |
| 单用户 | 没有鉴权，不要把端口暴露到公网 |

---

## 接下来

- [ ] 多轮会话（追问、上下文压缩）
- [ ] 图表工具（`plot` + 右侧产物面板）
- [ ] 会话历史与回放
- [ ] 更多数据源（CSV / Parquet / Postgres）

---

## 说明

这是一个以**学习和理解 agent 原理**为主的项目：循环、上下文、工具、事件流都是手写的，没有用 LangChain 之类的框架。每一步的设计取舍都写在了代码注释里。

初版方案文档见 [docs/PLAN.md](docs/PLAN.md)（部分内容已被实际实现取代）。
