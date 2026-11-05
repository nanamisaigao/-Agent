"""数据字典：这个项目里唯一"知道数据应该长什么样"的地方。

为什么把 schema 写成代码而不是写文档：
1. ingest 用它转换类型、建索引 —— 文档做不到；
2. agent 以后用它生成"表卡片" —— 一份声明两处使用，不会出现文档和库不一致；
3. 坑（note）跟着列走，看到列就看到坑，不会被忘在某个 README 里。

类型只有四种，映射到 SQLite：
    text     → TEXT        直接存字符串
    int      → INTEGER     整数
    float    → REAL        浮点（金额用 REAL，见下方说明）
    datetime → TEXT        存成 'YYYY-MM-DD HH:MM:SS'

关于时间：SQLite 没有日期类型，只能存文本。存 ISO8601 的好处是字符串排序 = 时间排序，
且 strftime('%Y-%m', col) 能直接按月分组。写 SQL 时不要假设有 DATE_TRUNC。
"""

from __future__ import annotations

from dataclasses import dataclass

SQL_TYPES = {"text": "TEXT", "int": "INTEGER", "float": "REAL", "datetime": "TEXT"}


@dataclass(frozen=True)
class Column:
    name: str
    kind: str                 # text / int / float / datetime
    nullable: bool = True
    note: str = ""


@dataclass(frozen=True)
class Table:
    name: str                 # 表名（去掉 olist_ 前缀和 _dataset 后缀）
    file: str                 # 源文件名
    columns: tuple[Column, ...]
    pk: tuple[str, ...] = ()  # 复合主键用 tuple，例如 ("order_id", "order_item_id")
    indexes: tuple[str, ...] = ()   # 除主键外还要建索引的列
    note: str = ""


@dataclass(frozen=True)
class DerivedTable:
    """由 SQL 生成的分析友好表，不对应任何 CSV。

    注意：不用 CREATE TABLE AS SELECT，因为它会让列类型声明丢失
    （SQLite 的 CTAS 无法推断表达式类型，PRAGMA 里会看到空类型）。
    所以这里是"显式建表 + INSERT SELECT"。
    """

    name: str
    columns: tuple[tuple[str, str], ...]   # (列名, SQL 类型)
    select_sql: str                        # 纯 SELECT（可以带 WITH）
    indexes: tuple[str, ...] = ()
    note: str = ""


def sql_type(kind: str) -> str:
    return SQL_TYPES[kind]


# --------------------------------------------------------------------------- #
# 9 张原始表。列顺序、类型都按 profile 结果人工确认过。
# --------------------------------------------------------------------------- #

TABLES: tuple[Table, ...] = (
    Table(
        name="customers",
        file="olist_customers_dataset.csv",
        columns=(
            Column("customer_id", "text", nullable=False,
                   note="每单一个客户号；做复购/用户维度分析必须用 customer_unique_id"),
            Column("customer_unique_id", "text", nullable=False,
                   note="真实客户 ID，同一人多次下单它是相同的"),
            Column("customer_zip_code_prefix", "int", nullable=False,
                   note="邮编前 5 位，可 join geolocation_zip"),
            Column("customer_city", "text", nullable=False),
            Column("customer_state", "text", nullable=False, note="巴西州缩写，27 个"),
        ),
        pk=("customer_id",),
        indexes=("customer_unique_id", "customer_zip_code_prefix"),
        note="99,441 行 = 99,441 个 customer_id，但只有 96,096 个真实客户",
    ),
    Table(
        name="orders",
        file="olist_orders_dataset.csv",
        columns=(
            Column("order_id", "text", nullable=False),
            Column("customer_id", "text", nullable=False,
                   note="外键 → customers.customer_id（注意不是真实客户）"),
            Column("order_status", "text", nullable=False,
                   note="8 种状态：delivered / shipped / canceled / unavailable 等"),
            Column("order_purchase_timestamp", "datetime", nullable=False,
                   note="下单时间，最常用的时间维度"),
            Column("order_approved_at", "datetime",
                   note="0.16% 空值，未审批的订单"),
            Column("order_delivered_carrier_date", "datetime",
                   note="1.79% 空值"),
            Column("order_delivered_customer_date", "datetime",
                   note="2.98% 空值 = 取消/未送达；算履约时长必须过滤 NULL"),
            Column("order_estimated_delivery_date", "datetime", nullable=False),
        ),
        pk=("order_id",),
        indexes=("customer_id", "order_purchase_timestamp"),
        note="时间跨度 2016-09 ~ 2018-10，但 2016 只有零星几单；可靠分析窗口是 2017-01 ~ 2018-08",
    ),
    Table(
        name="order_items",
        file="olist_order_items_dataset.csv",
        columns=(
            Column("order_id", "text", nullable=False),
            Column("order_item_id", "int", nullable=False,
                   note="同一单内的商品序号，从 1 开始"),
            Column("product_id", "text", nullable=False),
            Column("seller_id", "text", nullable=False),
            Column("shipping_limit_date", "datetime", nullable=False,
                   note="卖家发货截止时间（不是实际发货时间）"),
            Column("price", "float", nullable=False,
                   note="商品单价；REAL 存储，汇总时注意浮点误差，报告里要 ROUND"),
            Column("freight_value", "float", nullable=False, note="运费"),
        ),
        pk=("order_id", "order_item_id"),
        indexes=("product_id", "seller_id"),
        note="一行 = 一个商品行，不是一单。112,650 行对应 98,666 单，直接 COUNT(*) 会高估订单数",
    ),
    Table(
        name="order_payments",
        file="olist_order_payments_dataset.csv",
        columns=(
            Column("order_id", "text", nullable=False),
            Column("payment_sequential", "int", nullable=False,
                   note="同一单的第几笔支付，从 1 开始"),
            Column("payment_type", "text", nullable=False,
                   note="credit_card / boleto / voucher / debit_card / not_defined"),
            Column("payment_installments", "int", nullable=False,
                   note="分期数；有 0 值，含义是没定义而非零期"),
            Column("payment_value", "float", nullable=False, note="这一笔支付的金额"),
        ),
        pk=("order_id", "payment_sequential"),
        indexes=(),
        note="一单可能有多笔支付（组合支付），按单汇总要 SUM",
    ),
    Table(
        name="order_reviews",
        file="olist_order_reviews_dataset.csv",
        columns=(
            Column("review_id", "text", nullable=False,
                   note="注意：review_id 本身不唯一（98,410 个值 / 99,224 行）"),
            Column("order_id", "text", nullable=False),
            Column("review_score", "int", nullable=False, note="1~5 分"),
            Column("review_comment_title", "text",
                   note="88% 为空，别用它判断有没有评价"),
            Column("review_comment_message", "text",
                   note="59% 为空；葡萄牙语原文"),
            Column("review_creation_date", "datetime", nullable=False,
                   note="问卷发出时间，通常是收货后若干天"),
            Column("review_answer_timestamp", "datetime", nullable=False,
                   note="买家填答时间"),
        ),
        pk=("review_id", "order_id"),
        indexes=("order_id",),
        note="部分订单有重复评价；评价时间 ≠ 下单时间，按时间聚合时别混用",
    ),
    Table(
        name="products",
        file="olist_products_dataset.csv",
        columns=(
            Column("product_id", "text", nullable=False),
            Column("product_category_name", "text",
                   note="1.85% 空值（610 个商品无分类）；葡萄牙语分类名"),
            # 列名保留原始拼写错误 lenght，不顺手改，否则和源数据对不上
            Column("product_name_lenght", "int", note="源数据列名就拼错成 lenght"),
            Column("product_description_lenght", "int", note="同上"),
            Column("product_photos_qty", "int"),
            Column("product_weight_g", "int"),
            Column("product_length_cm", "int"),
            Column("product_height_cm", "int"),
            Column("product_width_cm", "int"),
        ),
        pk=("product_id",),
        indexes=("product_category_name",),
        note="尺寸重量字段有 0.01% 空值；分类名需 join 翻译表变英文",
    ),
    Table(
        name="sellers",
        file="olist_sellers_dataset.csv",
        columns=(
            Column("seller_id", "text", nullable=False),
            Column("seller_zip_code_prefix", "int", nullable=False),
            Column("seller_city", "text", nullable=False),
            Column("seller_state", "text", nullable=False),
        ),
        pk=("seller_id",),
        indexes=("seller_zip_code_prefix",),
        note="3,095 个卖家",
    ),
    Table(
        name="geolocation",
        file="olist_geolocation_dataset.csv",
        columns=(
            Column("geolocation_zip_code_prefix", "int", nullable=False),
            Column("geolocation_lat", "float", nullable=False),
            Column("geolocation_lng", "float", nullable=False),
            Column("geolocation_city", "text", nullable=False),
            Column("geolocation_state", "text", nullable=False),
        ),
        pk=(),
        indexes=("geolocation_zip_code_prefix",),
        note="100 万行、只有 19,015 个邮编，直接 join 会行爆炸；分析请用 geolocation_zip",
    ),
    Table(
        name="product_category_name_translation",
        file="product_category_name_translation.csv",
        columns=(
            Column("product_category_name", "text", nullable=False),
            Column("product_category_name_english", "text", nullable=False),
        ),
        pk=("product_category_name",),
        indexes=(),
        note="只有 71 条，但 products 里有 73 个分类 → 有分类翻译不出来，必须 LEFT JOIN",
    ),
)


# --------------------------------------------------------------------------- #
# 分析友好层：把 100 万行的原始坐标压成"一个邮编一行"。
# 直接 join geolocation 会让订单数翻几十倍，这是 Olist 最经典的坑。
# --------------------------------------------------------------------------- #

_GEO_ZIP_SQL = """
WITH counted AS (
    -- 第一步：按邮编+城市+州计数，一个邮编下会有多种写法
    SELECT geolocation_zip_code_prefix AS zip_code_prefix,
           geolocation_city           AS city,
           geolocation_state          AS state,
           COUNT(*)                   AS n_city
    FROM geolocation
    GROUP BY 1, 2, 3
),
ranked AS (
    -- 第二步：每个邮编取出现次数最多的那个城市（同票时按城市名排序，保证可复现）
    SELECT zip_code_prefix, city, state, n_city,
           ROW_NUMBER() OVER (PARTITION BY zip_code_prefix
                              ORDER BY n_city DESC, city) AS rn
    FROM counted
),
centers AS (
    -- 第三步：每个邮编取经纬度均值作为中心点
    SELECT geolocation_zip_code_prefix AS zip_code_prefix,
           AVG(geolocation_lat)        AS lat,
           AVG(geolocation_lng)        AS lng,
           COUNT(*)                    AS points
    FROM geolocation
    GROUP BY 1
)
SELECT c.zip_code_prefix, c.lat, c.lng, c.points, r.city, r.state
FROM centers c
LEFT JOIN ranked r
       ON r.zip_code_prefix = c.zip_code_prefix
      AND r.rn = 1
"""

DERIVED_TABLES: tuple[DerivedTable, ...] = (
    DerivedTable(
        name="geolocation_zip",
        columns=(
            ("zip_code_prefix", "INTEGER"),
            ("lat", "REAL"),
            ("lng", "REAL"),
            ("points", "INTEGER"),
            ("city", "TEXT"),
            ("state", "TEXT"),
        ),
        select_sql=_GEO_ZIP_SQL,
        indexes=("zip_code_prefix",),
        note="每个邮编一行：中心经纬度、样本点数、最常见的城市名。1 对 1 可安全 join",
    ),
)


# 表之间的外键关系，建索引和写文档都用它
RELATIONSHIPS: tuple[tuple[str, str, str, str], ...] = (
    ("orders", "customer_id", "customers", "customer_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("order_items", "seller_id", "sellers", "seller_id"),
    ("order_payments", "order_id", "orders", "order_id"),
    ("order_reviews", "order_id", "orders", "order_id"),
    ("products", "product_category_name",
     "product_category_name_translation", "product_category_name"),
    ("customers", "customer_zip_code_prefix", "geolocation_zip", "zip_code_prefix"),
    ("sellers", "seller_zip_code_prefix", "geolocation_zip", "zip_code_prefix"),
)
