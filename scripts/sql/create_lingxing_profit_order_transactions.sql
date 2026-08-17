-- 领星利润报表 · 订单维度 Transaction 本地仓
-- 在 ANALYTICS_DSN 对应库执行；示例：
--   psql "$ANALYTICS_DSN" -f scripts/sql/create_lingxing_profit_order_transactions.sql

CREATE TABLE IF NOT EXISTS lingxing_profit_order_transactions (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT NOT NULL DEFAULT '',
    source_row INTEGER NOT NULL DEFAULT 0,
    store_name TEXT,
    country TEXT,
    currency_code TEXT,
    order_datetime TIMESTAMPTZ,
    payment_datetime TIMESTAMPTZ,
    shipment_datetime TIMESTAMPTZ,
    posted_datetime TIMESTAMPTZ,
    fund_transfer_datetime TIMESTAMPTZ,
    deferred_datetime TIMESTAMPTZ,
    order_id TEXT,
    settlement_id TEXT,
    settlement_fid TEXT,
    msku TEXT,
    fnsku TEXT,
    asin TEXT,
    parent_asin TEXT,
    local_name TEXT,
    local_sku TEXT,
    account_type TEXT,
    event_source TEXT,
    fulfillment TEXT,
    description TEXT,
    settlement_status TEXT,
    fund_transfer_status TEXT,
    transaction_status TEXT,
    quantity NUMERIC(18, 4),
    principal_realname TEXT,
    product_developer_realname TEXT,
    product_sales NUMERIC(18, 4),
    product_sales_tax NUMERIC(18, 4),
    shipping_credits NUMERIC(18, 4),
    shipping_credits_tax NUMERIC(18, 4),
    giftwrap_credits NUMERIC(18, 4),
    giftwrap_credits_tax NUMERIC(18, 4),
    amazon_point_fee NUMERIC(18, 4),
    promotional_rebates NUMERIC(18, 4),
    promotional_rebates_tax NUMERIC(18, 4),
    sales_tax_collected NUMERIC(18, 4),
    low_value_goods NUMERIC(18, 4),
    marketplace_withheld_tax NUMERIC(18, 4),
    tcs_cgst NUMERIC(18, 4),
    tcs_sgst NUMERIC(18, 4),
    tcs_igst NUMERIC(18, 4),
    selling_fees NUMERIC(18, 4),
    fba_fees NUMERIC(18, 4),
    other_transaction_fees NUMERIC(18, 4),
    other_amount NUMERIC(18, 4),
    hidden_tax NUMERIC(18, 4),
    settlement_total NUMERIC(18, 4),
    purchase_costs_total NUMERIC(18, 4),
    logistics_costs_total NUMERIC(18, 4),
    other_costs_total NUMERIC(18, 4),
    custom_order_fee_total NUMERIC(18, 4),
    settlement_gross_profit NUMERIC(18, 4),
    settlement_gross_profit_rate NUMERIC(18, 6),
    promotion_id TEXT,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lx_profit_posted_datetime
    ON lingxing_profit_order_transactions (posted_datetime);

CREATE INDEX IF NOT EXISTS idx_lx_profit_currency_code
    ON lingxing_profit_order_transactions (currency_code);

CREATE INDEX IF NOT EXISTS idx_lx_profit_store_name
    ON lingxing_profit_order_transactions (store_name);

CREATE INDEX IF NOT EXISTS idx_lx_profit_order_id
    ON lingxing_profit_order_transactions (order_id);

CREATE INDEX IF NOT EXISTS idx_lx_profit_msku
    ON lingxing_profit_order_transactions (msku);

GRANT SELECT ON lingxing_profit_order_transactions TO amazon_finance_reader;
