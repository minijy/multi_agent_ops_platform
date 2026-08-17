---
name: profit-report-analysis
description: 本地 PostgreSQL 利润报表（领星订单 transaction 导入）查询指引
---

# 利润报表本地库分析

当用户询问已导入的利润报表、店铺汇总、MSKU 利润、订单毛利润时，优先使用 `profit_report_query`。

## 参数

- `metric`：overview / daily / store / msku / order / event_source
- `start_date` / `end_date`：YYYY-MM-DD，按结算时间过滤
- `currency_code`：USD、CNY 等；未指定则不限币种
- `store_name`：按店铺过滤（可选）

## 注意

- 数据来自表 `lingxing_profit_order_transactions`
- 若表为空，提示管理员运行 `scripts/import_lingxing_profit_xlsx.py` 导入 XLSX
- 会话已有同条件结果时优先复用
