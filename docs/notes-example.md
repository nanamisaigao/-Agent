# notes.md 写什么

每个工作区目录下都有一个 `notes.md`，它会原样拼进系统提示。自动分析能看出「`customer_id` 值唯一」，
但看不出「它其实是每单一个号、不是真客户」——这类业务知识只能人写。

下面是 Olist 数据集的实际内容，可以当模板抄：

```markdown
- `orders.customer_id` 是"每单一个号"，不是真实客户。用户维度必须用 `customers.customer_unique_id`。
- `order_items` 一行 = 一个商品行，不是一单。订单数用 `COUNT(DISTINCT order_id)` 或直接查 `orders`。
- 地址请用 `geolocation_zip`（每邮编一行）。直接 join 原始 `geolocation` 会让 10 万行变成 1500 万行。
- `order_delivered_customer_date` 有 3% 空值（取消/未送达），算履约时长要 `WHERE ... IS NOT NULL`。
- `products` 有 73 个分类，翻译表只有 71 条。分类汇总要 LEFT JOIN 并保留未翻译的。
- `order_payments` 一单可能有多笔（组合支付），按单汇总要 SUM。
- 可靠分析窗口：2017-01-01 ~ 2018-08-31（2016 年仅 300 余单，2018-09 后只有 20 单）。
```

## 写什么最有价值

| 类型 | 例子 |
|---|---|
| 指标口径 | "销售额 = `order_items.price` 之和，不含运费；含运费要用 `price + freight_value`" |
| 字段陷阱 | "`order_status` 里 `unavailable` 也是失败单，别只算 `canceled`" |
| 数据边界 | "2016 年的数据不完整，年度对比从 2017 开始" |
| 业务规则 | "同一买家多次下单算复购，用 `customer_unique_id` 去重" |

## 注意

`workspaces/` 整个目录都在 `.gitignore` 里——**这些笔记不会进版本库**，因为 `workspace.json`
里还存着本机绝对路径。要长期保存自己的笔记，建议单独备份这个文件。
