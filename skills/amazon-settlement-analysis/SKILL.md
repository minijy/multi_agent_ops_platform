---
name: amazon-settlement-analysis
description: 分析 Amazon RELEASED 结算交易、费用、SKU、交易类型和结算批次，并给出可核对结论。
model-invocable: true
user-invocable: true
---

# Amazon Settlement Analysis

使用 `amazon_finance_query` 获取事实，不要生成或执行任意 SQL。

## 工作顺序

1. 从问题提取指标、日期范围、seller、Top N 和分组维度。
2. seller 不明确且库中有多个 seller 时，先要求用户补充。
3. 调用 `amazon_finance_query`；查询范围固定为 `RELEASED`。
4. 回答中注明日期范围、币种、统计口径和返回行数。
5. 费用和退款不得混为销售收入；金额为 0 时不要推断为数据缺失。

## 对账规则

- 经营分析优先使用交易发生时间。
- 结算对账优先使用 settlement id 与 released 时间。
- 不同币种不得直接相加。
- Top N 结果必须说明排序依据。
