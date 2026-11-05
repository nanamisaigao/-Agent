# 数据分析 Agent —— 方案与学习路线

> **这份文档是最初的方案稿（由ai生成），部分内容已被实际实现取代**（例如 SQL 引擎最终用 SQLite 而不是 DuckDB，
> 目录布局从 `src/` 改成了根目录包，路线按功能推进而不是 S0-S7）。
> 当前的实际状态、用法和架构见 [README.md](../README.md)。
>
> 保留它的原因是：设计取舍的推演过程本身有价值。
>
> 环境基线：Windows / Python 3.13.16 / `D:\python\python_file\Agent` / PyCharm 2026.2.3 / Ollama 已装。

---

## 0. 先给结论（六个判断）

1. **别用 LangChain 起步。** 一个数据分析 agent 的主循环只有 200~300 行，手写一遍你才真正理解 tool calling、上下文压缩、错误回灌。框架留到 S5 之后按需替换。
2. **SQL 用 DuckDB，不用 MySQL/Postgres 起步。** 它能直接查 CSV / Parquet / pandas DataFrame，零部署，`duckdb.sql(...).df()` 一行回到 pandas。教学成本最低，且窗口函数、CTE 全支持。
3. **pandas 必须跑在"常驻 kernel"里，不能每步起新进程。** 数据分析天然是多步有状态的（load → clean → join → agg → plot）。每步重跑会浪费 token 且容易前后不一致。这一点连 JetBrains 官方在 2026.2 都给 agent 做了 Jupyter kernel 支持，方向是对的。
4. **"强"不体现在能写 SQL，而体现在三件事**：schema 理解、结果摘要（而不是把整张表塞进上下文）、失败后自我修正。方案里 80% 的设计精力应该花在这三处。
5. **PyCharm 接入分三档做，不要一上来搞 MCP。** 第一档只要 Run Configuration + debugpy 就有 80% 的价值；MCP 是锦上添花，且它的真正用法是**你的 agent 去调 PyCharm**，不是反过来。
6. **学习型项目必须有评测集。** 没有 20~30 道带标准答案的题，你无法判断"改进了"还是"改坏了"。这是最容易被跳过、也最值得先写的一步。

---

## 1. 目标与验收标准

### 1.1 功能目标

| 能力 | 验收标准（可测） |
|---|---|
| SQL | 对 ≥3 张表的库，能独立完成连接 + 聚合 + 窗口函数 + CTE；错误 SQL 能在 1 次内自我修正 |
| pandas | 能完成缺失值/重复/类型/时区/编码五类清洗；能在已有 DataFrame 上增量回答追问 |
| 可视化 | 根据问题**选对图型**（趋势/分布/对比/关系/占比），输出可读的中文标注图 |
| 报告 | 产出 markdown + notebook，每个数字都能追溯到某一步的工具返回 |
| PyCharm | 在 IDE 内一键运行；每步生成的代码可见、可改、可断点调试 |

### 1.2 学习目标（这才是"核心是学习性质"的含义）

每阶段结束你要能回答三个问题，答不上就回去补：

- **为什么这么设计**：比如"为什么结果要落盘成 parquet 再给句柄，而不是直接返回 DataFrame？"
- **换一种做法会怎样**：每阶段配一个 ablation 实验（见第 9 节），亲手对比数据。
- **边界在哪**：什么情况下它会失败？失败时是静默错还是报错？

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│  交互层  CLI(typer) │ Jupyter magic │ HTTP(FastAPI) │ MCP server │
└───────────────────────────┬──────────────────────────────────┘
                            │  用户问题 + 会话
┌───────────────────────────▼──────────────────────────────────┐
│  编排层  Agent Loop (手写状态机)                               │
│  规划 → 选工具 → 执行 → 观察 → 修正 → 汇总                      │
│  上下文管理：schema 卡片 / 已确认事实 / 产物句柄 / 历史压缩      │
└──────┬───────────────────────────────────────────┬───────────┘
       │ 工具调用（JSON Schema 强约束）              │ 只读
┌──────▼──────────────────┐              ┌─────────▼──────────┐
│  工具层                  │              │  上下文层           │
│  run_sql / run_python   │              │  schema_cards.json │
│  profile / plot         │              │  facts.md          │
│  read_file / save_artifact             │  artifacts 索引     │
└──────┬──────────────────┘              └────────────────────┘
       │
┌──────▼───────────────────────────────────────────────────────┐
│  执行层（隔离）                                                │
│  常驻 Jupyter kernel (ipykernel)  ←→  DuckDB (in-process)     │
│  超时 / 禁止导入 / 只读数据目录 / 结果落盘                      │
└──────────────────────────────────────────────────────────────┘
       │
┌──────▼───────────────────────────────────────────────────────┐
│  数据层   data/raw(只读) │ duckdb 视图 │ artifacts/ │ notebooks/ │
└──────────────────────────────────────────────────────────────┘
```

**关键解耦**：执行环境（kernel）与推理循环（loop）彻底分离。loop 只发"代码字符串 + 超时"，kernel 只回"stdout / 异常 / 变量快照"。这样换模型、换界面、换沙箱都不影响主循环。

---

## 3. 技术选型

| 关注点 | 选择 | 理由 | 被否方案 |
|---|---|---|---|
| SQL 引擎 | **DuckDB** | 零部署；直接 `read_parquet/read_csv`；`duckdb.sql()` ↔ DataFrame 零拷贝；支持窗口/CTE/`EXPLAIN` | SQLite（窗口函数弱）、Postgres（要起服务） |
| 执行内核 | **jupyter_client + ipykernel** | 状态持久、异常结构清晰、支持中断、与 PyCharm 共用协议 | `exec()` 裸执行（状态难管）、每步 subprocess（慢且丢状态） |
| 数据框 | **pandas 2.x + pyarrow** | 生态、`dtype` 显式化 | polars（后续可加，先不引入第二套心智） |
| 静态图 | **matplotlib (Agg)** | 稳定、可复现、评审友好 | seaborn（只做封装，按需） |
| 交互图 | **plotly → HTML** | 落盘即用，不依赖前端 | — |
| 模型 | **OpenAI 兼容接口**（DeepSeek 等），可切 **Ollama 本地** | 函数调用稳定；本地模型可离线/保隐私 | 直接绑某家 SDK（锁死） |
| CLI | typer + rich | 几十行搞定，回显好看 | argparse |
| 校验 | pydantic | 工具入参/返回值统一校验 | 手写校验 |
| 可观测 | 自写 JSONL trace | 每一步 prompt/token/耗时都留痕，便于复盘 | LangSmith（先不引第三方） |
| 编排框架 | **暂时不用** | 手写掌握原理；S5 后可换 LangGraph 做 checkpoint/中断恢复 | LangChain / CrewAI |

> 依赖清单（仅规划，暂不安装）：`duckdb pandas pyarrow matplotlib plotly jupyter_client ipykernel openai pydantic typer rich chardet pyyaml`。

---

## 4. 目录结构

```
Agent/
├─ data/
│  ├─ raw/                 # 只读，原始数据（csv/parquet/sqlite）
│  └─ curated/             # 清洗后的中间层
├─ src/da_agent/
│  ├─ cli.py               # typer 入口：ask / chat / eval / profile
│  ├─ loop.py              # 主循环（S2 起）
│  ├─ llm.py               # 模型客户端 + 重试 + 计费
│  ├─ schema_tools.py      # 工具 JSON Schema 定义（单一事实来源）
│  ├─ tools/
│  │  ├─ sql.py            # run_sql / list_tables / describe_table
│  │  ├─ python_exec.py    # run_python（基于 kernel）
│  │  ├─ profile.py        # 列级画像：缺失/基数/分布/异常
│  │  ├─ viz.py            # plot：图型校验 + 落盘
│  │  └─ artifacts.py      # 句柄读写
│  ├─ runtime/
│  │  ├─ kernel.py         # kernel 生命周期、执行、超时、中断
│  │  ├─ policy.py         # 导入白名单 / 危险调用拦截（静态 + 运行时）
│  │  └─ duck.py           # DuckDB 连接、只读视图、方言适配
│  ├─ context/
│  │  ├─ builder.py        # 组装 prompt：schema + facts + 历史摘要
│  │  └─ facts.py          # 已确认事实库（带 provenance）
│  ├─ report.py            # 报告生成：md / ipynb / html
│  └─ eval/
│     ├─ cases/*.yaml      # 题目 + 标准答案 + 断言
│     └─ runner.py         # 跑分、对比、回归
├─ integrations/
│  └─ pycharm/
│     ├─ mcp_server.py     # 把本 agent 的工具暴露给 IDE（可选，S6）
│     └─ ide_client.py     # 作为 MCP 客户端调用 PyCharm（可选，S6）
├─ notebooks/              # 每个会话自动落一个 .ipynb，可直接在 PyCharm 打开
├─ artifacts/              # 图、parquet、报告（gitignore）
├─ evals/                  # 跑分结果（json + 对比 md）
└─ tests/                  # 工具层单测（不打模型，纯确定性）
```

---

## 5. 工具契约（最重要的设计）

**统一返回信封**（所有工具必须遵守）：

```json
{
  "ok": true,
  "tool": "run_sql",
  "elapsed_ms": 42,
  "result": {
    "columns": ["month", "gmv"],
    "dtypes": ["VARCHAR", "DOUBLE"],
    "row_count": 12,
    "truncated": false,
    "preview_md": "| month | gmv |\n|---|---|\n| 2026-01 | 1200 |",
    "handle": "artifacts/q_8f3a.parquet"
  },
  "notes": ["duckdb 1.x, dialect=duckdb"],
  "error": null
}
```

**三条铁律**：

1. **回上下文的是摘要，不是数据**：预览 ≤ 30 行 / ≤ 4KB；全量写 parquet，给 `handle`，后续 SQL 可以直接 `FROM 'artifacts/q_8f3a.parquet'`。这一条决定了 token 成本量级。
2. **错误必须结构化可回灌**：`error: {type, message, hint, position}`。SQL 报错要带上"你写的是 `GROUP BY 1`，但 SELECT 里第 1 列是聚合函数"这类可操作信息。
3. **数字只能来自工具返回**：报告生成时做 grounding 校验，正文里的每个数字都要能在 facts 库中找到来源 step，否则报"未溯源"。

### 工具清单

| 工具 | 入参 | 用途 | 防护 |
|---|---|---|---|
| `list_tables` | — | 列出表/视图 + 行数 | — |
| `describe_table` | `table` | 列名、类型、样例、基数 | 只读 |
| `profile_column` | `table, column` | 缺失率/唯一值/前后 5 分位 | 只读 |
| `run_sql` | `sql, limit=200` | 执行 SQL（DuckDB） | 只允许单条 `SELECT/WITH`；外层包 `LIMIT`；`EXPLAIN` 预检；超时 30s |
| `run_python` | `code, timeout=60` | 在常驻 kernel 执行 | 禁 `os/subprocess/socket/open(写)`；超时中断；捕获 stdout/last_expr |
| `plot` | `spec{kind,x,y,group,title}` 或 `code` | 出图 | 白名单图型；强制 `savefig` + `plt.close`；中文字体 |
| `save_artifact` | `name, kind` | 登记产物 | 路径白名单 |
| `read_file` | `path, nrows` | 读 csv/excel/json | 只在 `data/` 内 |

> 为什么不给 agent 一个通用的"随便跑 Python"？因为**约束越窄，成功率越高**。`run_sql` 比"用 pandas 写 SQL 等价物"可靠得多，且更省 token。

---

## 6. 执行内核设计（最容易被低估的部分）

```
KernelManager (单例)
├─ start(): ipykernel, 固定 cwd=data/, 清空危险环境变量
├─ run(code, timeout) -> {status, stdout, result_repr, error, duration}
│    · 执行前：ast 解析 → 策略检查（导入白名单、禁止 dunder 逃逸）
│    · 执行中：`execute_interactive`，超时则 `interrupt_kernel`
│    · 执行后：只取最后一行表达式 + stdout 尾部 200 行
├─ snapshot(names) -> 变量清单（名字/类型/shape/内存）
└─ restart(): 状态坏了能一键重来
```

必须处理的细节：

- **超时后 kernel 可能仍卡住** → 先 `interrupt`，3 次失败则 `restart` 并在 facts 里记"环境已重置"。
- **大对象内存**：每次执行后检查 `df.memory_usage()` 总和，超过阈值提示 agent "请先聚合/落盘"。
- **中文字体**：`mpl.rcParams['font.sans-serif'] = ['Microsoft YaHei']`、`axes.unicode_minus=False`，否则图上全是豆腐块。
- **CSV 编码**：Windows 上大量文件是 GBK，读取时用 `chardet` 探测，别默认 utf-8。
- **安全性坦白**：白名单是"防手滑"，不是"防恶意"。真要跑不可信代码得用容器/独立用户，本项目的学习目标不含这个，文档里写清楚，别自我欺骗。

---

## 7. 上下文与"分析笔记"

agent 的强弱一半在上下文工程。四类内容分不同寿命管理：

| 内容 | 生命 | 存放 | 压缩策略 |
|---|---|---|---|
| 数据字典 / schema 卡片 | 全会话 | 启动时由 profile 生成，缓存到 `context/schema_cards.json` | 不压缩，稳定前缀利于命中缓存 |
| 已确认事实 | 全会话 | `facts.md`，每条带 `step_id + 工具 + 关键数字` | 只增不改，冲突时显式标注"口径变更" |
| 对话历史 | 滚动 | 最近 6~10 轮原文 | 更早的压缩成一句话 + 指回 step |
| 产物句柄 | 全会话 | artifacts 索引 | 只留名字 + 形状 + 用途 |

**开场必须先做口径澄清**：真实分析里多数错误不是 SQL 写错，是"销售额"含不含退款、"活跃"怎么定义。所以 agent 的第一步固定输出 2~3 个澄清问题 + 一个执行计划，用户确认后再动手。这个习惯比任何 prompt 技巧都值钱。

---

## 8. PyCharm 接入（三档，按投入从小到大）

你的版本是 **PyCharm 2026.2.3**，这三档都具备条件。

### A 档：Run Configuration + debugpy（半天，先做）

- `.run/` 下放两个配置：
  - `agent: ask` → `python -m da_agent.cli ask "$Prompt"`
  - `agent: debug` → `python -m debugpy --listen 5678 -m da_agent.cli chat`
- PyCharm 用 **Attach to Process / Remote Debug**（端口 5678）挂上去，就能**单步跟一次完整的 agent 循环**：看 messages 数组怎么长、工具 schema 怎么被模型挑中、错误怎么回灌。
- **学习价值最高的其实是这个**：调试器里看上下文，比读十篇文章都直观。

### B 档：共用一个 Jupyter kernel（1~2 天，核心）

- agent 自己起一个 Jupyter server：`jupyter server --no-browser --port 8888`。
- PyCharm 连到同一个 server（Settings → Jupyter Server，填 URL + token）。
  - **前置确认**：这需要 PyCharm 的 Jupyter 支持可用（社区版历史上没有，2026 统一版需实测一下菜单里有没有 "Jupyter Server"）。若不可用，退化为 B′：agent 每步把 cell 追加写入 `notebooks/session-<ts>.ipynb`，你在 IDE 里打开就能看到完整分析过程、改一行重跑。
- 每步执行后同步落盘 notebook，**cell 是给人读的**（带 markdown 说明），不是 raw code dump。
- 额外好处：kernel 里的 DataFrame 和你 IDE 里的是**同一个对象**，你可以在 PyCharm 里直接 `df.head()` 检查 agent 看到的数据对不对。这是调试 agent 幻觉最快的手段。

### C 档：MCP 双向（3~5 天，可选）

PyCharm 2025.2 起内置 MCP Server（Settings → Tools → MCP Server），把 IDE 能力开放给外部 agent。工具包括：`execute_run_configuration`、`execute_terminal_command`、`open_file_in_editor`、`get_file_problems`、`replace_text_in_file`、`create_new_file` 等。

**C1：你的 agent 当客户端去调 IDE**（推荐方向）

```
agent 写完一段分析脚本
  → create_new_file("notebooks/analysis_042.py")
  → get_file_problems(...)        # 先让 IDE 静态检查，语法/未定义变量
  → execute_run_configuration("reproduce")  # 跑一遍验证可复现
  → open_file_in_editor(...)      # 在编辑器里弹出结果
```

价值：agent 的产出天然进入 IDE 工作流，而且"用 IDE 的检查器兜底"比自己在 prompt 里强调"别写错"有效。

**C2：反向，把 agent 工具包成 MCP server 给 IDE 的 AI 面板用**

- 实现 `integrations/pycharm/mcp_server.py`，暴露 `run_sql` / `run_python` / `profile`。
- 在 Settings → Tools → MCP Server 里复制 Stdio 配置，粘到支持 MCP 客户端的插件/助手配置中。
- 这条路可行性取决于你装的具体 AI 插件是否支持 MCP 客户端，**S6 阶段再实测**，不作为主线。

> 另外：2026.2.1 起 JetBrains 官方给 PyCharm 加了 Jupyter kernel 的 agent 能力（需 AI 订阅）。这说明 B 档的设计方向是对的——官方也在解决"agent 别在一次性 shell 里丢状态"的问题。

---

## 9. 学习路线图（8 周 × 6~8 小时）

每个阶段都必须**可运行、可验证、可回看**，不允许"写了一半没跑通就往下走"。

### S0（第 1 周）基线与"反例"
- **做**：装依赖；导入 Chinook（SQL 练习库）+ 一份 Superstore 销售数据；手写一个 80 行的硬编码分析脚本（固定 SQL + 固定图），跑出 3 张图 + 1 份结论。
- **产出**：`notebooks/s0_manual.ipynb`。
- **学**：把"人怎么做分析"固定成流程，你才知道 agent 要复制什么、哪一步最脆弱。
- **验收**：能不看文档复现整个流程。
- **自测**：如果换成另一份同结构数据，你的脚本有几处要改？（答案通常是 4~6 处——这就是 agent 要自动化的部分）

### S1（第 1~2 周）执行内核（不接模型）
- **做**：`runtime/kernel.py` + `tools/sql.py` + `tools/python_exec.py`；CLI 支持 `da-agent exec --sql "..."` / `--py "..."`。
- **产出**：能连续三步共享 DataFrame 的交互式 REPL。
- **学**：Jupyter 消息协议、有状态执行、超时与中断、返回值序列化。
- **验收**：`df = pd.read_csv(...)` → `df.head()` → `df.groupby(...)` 三步在同一个 kernel 且第二步能看到第一步变量；故意写个死循环，10s 后能自动中断并继续用。
- **自测**：kernel 重启后上下文里哪些东西失效了？你怎么让 agent 知道？
- **ablation**：对比"每步新进程"的耗时和失败率。

### S2（第 2~3 周）单工具闭环（接入模型）
- **做**：`llm.py` + `loop.py` 最小版 + 3 个工具（`list_tables`/`describe_table`/`run_sql`）。
- **产出**：能回答"去年各月 GMV 趋势"这类单表/单 join 问题。
- **学**：function calling 的 schema 设计、tool_choice、错误回灌、重试与降级。
- **验收**：20 道单表题 ≥ 70% 一次通过；SQL 错的能在 2 轮内自修。
- **自测**：为什么工具描述里的 `description` 比参数名更重要？把 `run_sql` 的描述删掉一半，成功率掉多少？（做这个实验）

### S3（第 3~4 周）多步规划与自我修正
- **做**：plan-execute 模式；`profile_column`；SQL 预检（`EXPLAIN` + 空结果检测）；pandas 执行前静态检查；facts 库。
- **产出**：能完成"清洗 → 计算 → 对比"的多步任务。
- **学**：ReAct vs Plan-Execute 的取舍、什么时候该停下来问人、如何避免循环打转（连续 2 次同类失败就换策略）。
- **验收**：10 道多步题 ≥ 60% 端到端完成；平均工具调用 ≤ 6 次。
- **自测**：为什么"先出计划再执行"在数据分析里通常优于纯 ReAct？（提示：口径歧义）

### S4（第 4~5 周）可视化与报告
- **做**：`plot` 工具（图型白名单 + 图型选择规则）、报告生成（md/ipynb）、grounding 校验。
- **产出**：一次问答产出一份带 3 张图、每个数字可溯源的分析报告。
- **学**：图型与问题的映射、配色与标注规范、报告叙事结构（结论先行）。
- **验收**：人工盲评 10 份报告，≥7 份"能直接用"。
- **自测**：agent 选了折线图但数据是分类的，你怎么让它在**生成前**而不是**生成后**发现？

### S5（第 5~6 周）评测与回归
- **做**：24~30 道题的评测集（4 类：单表聚合 / 多表连接+窗口 / 数据清洗 / 图表选择）；`runner.py`；`evals/` 落盘对比。
- **指标**：执行成功率、SQL 结果正确率（golden 单元格对比）、端到端完成率、平均轮数、token/任务。
- **学**：怎么定义"对"、怎么防止自己调题、回归测试怎么做。
- **验收**：一次改动前后能出一张对比表。
- **自测**：哪类题最差？是模型能力问题还是工具设计问题？（用固定模型+改工具 vs 固定工具+换模型，分离变量）

### S6（第 6~7 周）IDE 集成与可观测
- **做**：A 档 → B 档 → C1；JSONL trace + 一个简单的 trace 查看页（或直接渲染成 md）。
- **产出**：在 PyCharm 里一键跑、能断点、能看 trace。
- **学**：MCP 协议、进程间通信、trace 设计（哪 6 个字段最值钱）。
- **验收**：从 IDE 里跑一个复杂问题，全程不用切终端。
- **自测**：MCP 相比直接调子进程，多解决了什么问题？（答案：IDE 的语义能力，如检查器、运行配置、编辑器状态）

### S7（第 7~8 周）收口
- **做**：README、架构图、限制说明、演示脚本；选一个真实数据集做一次完整分析并写复盘。
- **学**：怎么向别人解释你的设计取舍。

---

## 10. 评测设计

| 维度 | 方法 | 阈值（初版目标） |
|---|---|---|
| SQL 正确性 | 与 golden SQL 结果做单元格级比对（排序、浮点容差、列名规范化） | ≥ 80% |
| 端到端完成 | 人工/规则判定"回答了问题" | ≥ 60% |
| 效率 | 平均工具调用轮数、token/任务 | ≤ 6 轮，≤ 25k tokens |
| 可复现 | 同一问题跑 3 次，结果一致性 | ≥ 2/3 一致 |
| 可读性 | 报告盲评 | ≥ 7/10 |

**防作弊**：评测题只给自己的 agent 用，不要放进 prompt 的 few-shot 里；每次改代码都跑全量，记录版本号。

---

## 11. 数据素材（对口练习）

| 数据集 | 练什么 |
|---|---|
| Chinook（SQLite 官方示例） | 多表连接、窗口函数、`WITH` 递归 |
| Northwind | 经典业务口径、日期维度 |
| Superstore 销售 | pandas 清洗 + 可视化 + 归因 |
| 一份自造"脏数据" | 缺失/重复/类型错乱/时区混用/GBK 编码/口径歧义 |
| 大一点的 parquet（如 taxi 样本） | 性能、下推、结果落盘 |

> 建议第 4 类一定要自己做：真实痛点集中在"数据不干净"和"口径不清"，公开数据集太干净，会让你误判 agent 的能力。

---

## 12. 已知的坑（提前写下来，省得到时踩）

1. **上下文爆炸**：不加限制，一个 `SELECT *` 就能把 200k token 打满。→ 强制摘要 + 句柄。
2. **数字幻觉**：模型会把"看起来合理"的数字写进结论。→ grounding 校验（报告里每个数字回查 facts）。
3. **静默错误**：SQL 跑通了但 join 错了导致行数翻倍。→ 每次 join 后自动校验基数和行数变化，异常就警告。
4. **方言混淆**：DuckDB 的 `QUALIFY`、`::` 转换、日期函数和 Postgres 不同。→ 在 schema 卡片里写明方言，工具返回 `notes` 带方言名。
5. **Windows 编码**：GBK CSV、路径空格、matplotlib 中文。→ 统一处理，别分散在各处。
6. **超时与僵尸 kernel**：中断不彻底会污染后续分析。→ 三次失败就 restart 并显式记账。
7. **过度信任**：agent 说"数据表明 A 导致 B"其实只是相关。→ 报告模板里加"局限与替代解释"一节，强制填。
8. **成本失控**：多步任务 token 增长是超线性的。→ 每步记录 token，超预算就降级到"只返回摘要"。

---

## 13. 时间与里程碑

| 周 | 阶段 | 里程碑（可演示） |
|---|---|---|
| 1 | S0 | 手写分析脚本复现完整流程 |
| 1-2 | S1 | 有状态执行内核 + 超时中断可用 |
| 2-3 | S2 | 单表问答 70% 通过 |
| 3-4 | S3 | 多步任务 60% 完成 |
| 4-5 | S4 | 产出可溯源报告 |
| 5-6 | S5 | 评测集 + 对比表 |
| 6-7 | S6 | PyCharm 内一键跑通 |
| 7-8 | S7 | 一次真实数据分析 + 复盘 |

---

## 14. 动手前需要你定的三件事

1. **模型通道**：DeepSeek API（便宜、函数调用稳）／OpenAI 兼容的其他家／本地 Ollama（离线、隐私，但工具调用质量要看具体模型）。—— 影响 `llm.py` 和评测基线。
2. **PyCharm 的 Jupyter 能力**：你的 2026.2.3 里 Settings 能不能看到 "Jupyter Server"？决定 B 档还是 B′ 档。
3. **练习数据集**：用我的推荐组合（Chinook + Superstore + 自造脏数据），还是你手上已有真实业务数据？（有真实数据更好，但要先脱敏）

这三件事定了，第一步就是 S0 —— 不写 agent 代码，先手写一遍分析。

---

## 15. Web 前端（2026-11 新增需求）

目标：像 DSH 那样，在一个网页里看到 agent 的每一步——说了什么、跑了什么 SQL、结果长什么样、出了什么图。

### 15.1 它会改变架构的地方：一个内核，多个前端

**不能把界面逻辑写进 agent 里。** 否则将来加 CLI、Jupyter、PyCharm 就要各写一遍，四处不一致。

正确做法：**agent 只产出事件流，界面只渲染事件流。**

```
        ┌──────────────┐
        │  agent loop  │  只做推理，产出事件
        └──────┬───────┘
               │  events (JSON)
     ┌─────────┼─────────┬──────────┐
     ▼         ▼         ▼          ▼
   CLI     Web(SSE)   Notebook   PyCharm
```

这也是 DSH 自己的做法：同一个内核，多个渲染层。做好这一点，前端就从"再写一个程序"变成"换一层皮肤"。

### 15.2 事件协议（前端、后端、CLI 的共同契约）

| 事件 | 关键字段 | 界面表现 |
|---|---|---|
| `run_start` | `run_id`, `question` | 用户气泡 + 时间 |
| `plan` | `steps[]` | 计划卡片（可勾选） |
| `text` | `text` | 打字机式追加 |
| `tool_call` | `id`, `name`, `args` | 工具卡片，折叠，标题是"跑了 run_sql" |
| `tool_result` | `id`, `ok`, `preview_md`, `meta{rows,ms,truncated}`, `hint` | 卡片展开：SQL + 结果表 + 耗时 |
| `artifact` | `id`, `kind`, `path`, `caption` | 图片 / 文件卡片 |
| `answer` | `text` | 正文回答 |
| `run_end` | `usage`, `ms` | 底部统计条 |
| `error` | `message`, `hint` | 红色卡片 |

一条原则：**事件必须是纯 JSON、可序列化、无副作用**。这样它既能走 SSE，也能写进 JSONL 做回放和评测。

### 15.3 后端：FastAPI + SSE

| 路由 | 用途 |
|---|---|
| `POST /api/ask` | 发起一次提问，返回 `text/event-stream` |
| `GET /api/tables` | 表清单 + 行数（左侧栏） |
| `GET /api/table/{name}` | 单表结构（列/类型/注释） |
| `GET /api/artifacts/{path}` | 图片、导出文件 |
| `GET /api/sessions` | 会话列表 |
| `GET /` | 静态页 |

**为什么用 SSE 而不是 WebSocket**：这一场景只需要单向下行，SSE 是普通 HTTP、自带断线重连语义、用 `curl` 就能调试。WebSocket 是双向的，这里用不上，反而多一层状态管理。

依赖只加两个：`fastapi`、`uvicorn`。SSE 用 `StreamingResponse` 手写，不引第三方库。

### 15.4 前端：先不引入构建工具

```
web/
├─ index.html          三栏骨架
├─ style.css           暗色主题
└─ js/
   ├─ app.js           入口：输入框、发起请求、读流
   ├─ sse.js           解析 event-stream（约 40 行）
   ├─ dispatch.js      事件 → 组件 的分发
   └─ cards.js         消息卡 / 工具卡 / 图表卡
```

**为什么 v1 不用 React**：你现在要学的是"事件流怎么变成界面"，不是打包工具链。原生 ES module 浏览器直接跑，改一行刷新就见效，没有构建等待。等你觉得 DOM 操作烦了，再迁 React——那时你已经知道该抽象什么。

三栏布局：

```
┌──────────┬──────────────────────────┬────────────┐
│ 表清单   │  对话流                   │ 产物       │
│ 会话历史 │  ├ 用户问题               │ 图 / 导出  │
│          │  ├ 工具卡片（SQL+结果）    │ 文件       │
│          │  └ 回答                   │            │
└──────────┴──────────────────────────┴────────────┘
```

### 15.5 和 DSH 的差距（别过度承诺）

v1 **做**：聊天、流式文本、工具卡片（可展开看 SQL 和结果）、图表、表清单。

v1 **不做**：多会话并行、文件 diff、IDE 联动、插件系统、权限确认弹窗。这些等前面跑顺了再说。

### 15.6 实施顺序

| 步骤 | 内容 | 验收 |
|---|---|---|
| 4 | agent loop + 工具层 | CLI 里问一句话，能自动选 `run_sql` 并回答 |
| 5 | 事件流 + FastAPI（先用假事件） | `curl -N` 能看到事件一行行吐出来 |
| 6 | 前端三栏 | 浏览器里完成一次提问，看到工具卡片 |
| 7 | 图表工具 + 产物面板 | 出一张图并在右侧显示 |

**为什么 loop 在 UI 前面**：事件协议的形状是被 loop 决定的。先写 UI 会照着想象设计协议，回头 loop 一改就要返工两遍。

---

## 16. 修订后的实际进度

| 步骤 | 状态 | 内容 |
|---|---|---|
| 1 | 完成 | 数据画像 `profile` |
| 2 | 完成 | 数据字典 + 建库 `ingest`（SQLite，10 表 156 万行） |
| 3 | 完成 | 只读查询层 `run_sql`（围栏 / 只读 / 超时 / 错误提示） |
| 4 | 下一步 | agent loop + 工具层（需要先定模型通道） |
| 5 | 计划 | 事件流 + FastAPI |
| 6 | 计划 | Web 前端三栏 |
| 7 | 计划 | 图表工具 + 产物面板 |

