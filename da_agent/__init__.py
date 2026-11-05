"""da_agent —— 数据分析 agent。

分层约定（后面每一步都按这个顺序加）：
    config   路径与配置，唯一知道"文件在哪"的地方
    schema   数据字典，唯一知道"数据长什么样"的地方
    inspect  看数据（画像），不写数据
    ingest   把原始文件变成可查询的库
    tools    给 agent 用的工具（run_sql / run_python / plot ...）
    loop     推理循环
"""

__version__ = "0.1.0"
