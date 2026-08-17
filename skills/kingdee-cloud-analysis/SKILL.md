---
name: kingdee-cloud-analysis
description: 金蝶云星空销售/应收单据 ExecuteBillQuery 查询指引
---

# 金蝶云星空单据分析

当用户询问金蝶 ERP 中的销售订单、销售出库、应收单、费用应收单时，使用 `kingdee_cloud_query`。

## 必填参数

- `document_type`：
  - `sale_order` 销售订单
  - `sale_outstock` 销售出库单
  - `ar_receivable` 普通应收单
  - `ar_expense_receivable` 费用应收单
- `start_date` / `end_date`：YYYY-MM-DD

## 可选参数

- `bill_no`：指定单据编号
- `limit`：返回条数，默认 50

## 注意

- WebAPI 凭证由管理员在「金蝶云星空 Agent」配置中维护
- 私有云地址需以 `/K3Cloud` 结尾，例如 `https://erp.example.com/K3Cloud`
- 若会话已有同区间查询结果，优先复用，不要重复拉取
