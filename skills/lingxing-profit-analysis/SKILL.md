---
name: lingxing-profit-analysis
description: 领星 ERP 利润报表（订单维度 transaction 视图）查询指引
---

# 领星利润报表分析

当用户询问店铺利润、订单维度 transaction、结算毛利润、平台费/FBA 费时，使用 `lingxing_profit_query`。

## 必填参数

- `start_date` / `end_date`：YYYY-MM-DD
- 用户给出币种时设置 `currency_code`（如 USD、CNY）；未指定则留空（原币种）

## 注意

- 凭证由管理员在「领星开放平台 Agent」配置中维护，工具侧无需传 App ID/Secret
- 默认 `search_date_field=posted_date_locale`（结算时间）
- 默认 `order_status=Disbursed`（已发放）
- 若会话已有同区间查询结果，优先复用，不要重复拉取
