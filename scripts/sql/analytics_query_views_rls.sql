-- Analytics query plane: tenant column, non-PII views, RLS, reader grants.
-- Run on ANALYTICS_DSN (PostgreSQL 15+ for security_invoker views):
--   psql "$ANALYTICS_DSN" -f scripts/sql/analytics_query_views_rls.sql
--
-- Backfill before serving traffic:
--   UPDATE amazon_finance_transactions SET tenant_id = '<tenant>' WHERE tenant_id IS NULL OR tenant_id = '';
--   UPDATE lingxing_profit_order_transactions SET tenant_id = '<tenant>' WHERE tenant_id IS NULL OR tenant_id = '';
-- The application always binds tenant_id from Runtime, never from the model.

BEGIN;

ALTER TABLE amazon_finance_transactions
    ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE lingxing_profit_order_transactions
    ADD COLUMN IF NOT EXISTS tenant_id TEXT;

CREATE INDEX IF NOT EXISTS idx_amazon_finance_transactions_tenant
    ON amazon_finance_transactions (tenant_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_lx_profit_tenant_posted
    ON lingxing_profit_order_transactions (tenant_id, posted_datetime);

CREATE OR REPLACE VIEW amazon_finance_released_transactions
WITH (security_invoker = true) AS
SELECT seller_id,
       transaction_id,
       tenant_id,
       marketplace_id,
       account_type,
       transaction_status,
       transaction_type,
       posted_at,
       currency_code,
       total_amount
FROM amazon_finance_transactions
WHERE transaction_status = 'RELEASED';

CREATE OR REPLACE VIEW amazon_finance_released_items
WITH (security_invoker = true) AS
SELECT i.seller_id,
       i.transaction_id,
       t.tenant_id,
       i.sku,
       i.asin,
       i.quantity_shipped,
       i.fulfillment_network,
       i.currency_code,
       i.total_amount
FROM amazon_finance_items i
JOIN amazon_finance_transactions t
  ON t.seller_id = i.seller_id AND t.transaction_id = i.transaction_id
WHERE t.transaction_status = 'RELEASED';

CREATE OR REPLACE VIEW amazon_finance_released_amount_lines
WITH (security_invoker = true) AS
SELECT l.seller_id,
       l.transaction_id,
       t.tenant_id,
       l.source_scope,
       l.category_level_1,
       l.category_level_2,
       l.category_level_3,
       l.breakdown_type,
       l.currency_code,
       l.amount
FROM amazon_finance_amount_lines l
JOIN amazon_finance_transactions t
  ON t.seller_id = l.seller_id AND t.transaction_id = l.transaction_id
WHERE t.transaction_status = 'RELEASED';

CREATE OR REPLACE VIEW amazon_finance_released_identifiers
WITH (security_invoker = true) AS
SELECT ids.seller_id,
       ids.transaction_id,
       t.tenant_id,
       ids.identifier_name,
       ids.identifier_value
FROM amazon_finance_transaction_identifiers ids
JOIN amazon_finance_transactions t
  ON t.seller_id = ids.seller_id AND t.transaction_id = ids.transaction_id
WHERE t.transaction_status = 'RELEASED';

CREATE OR REPLACE VIEW lingxing_profit_report_query
WITH (security_invoker = true) AS
SELECT tenant_id,
       store_name,
       country,
       currency_code,
       posted_datetime,
       order_id,
       settlement_id,
       msku,
       asin,
       event_source,
       quantity,
       product_sales,
       selling_fees,
       fba_fees,
       other_transaction_fees,
       settlement_total,
       purchase_costs_total,
       logistics_costs_total,
       other_costs_total,
       settlement_gross_profit,
       settlement_gross_profit_rate
FROM lingxing_profit_order_transactions;

ALTER TABLE amazon_finance_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE amazon_finance_transactions FORCE ROW LEVEL SECURITY;
ALTER TABLE amazon_finance_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE amazon_finance_items FORCE ROW LEVEL SECURITY;
ALTER TABLE amazon_finance_amount_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE amazon_finance_amount_lines FORCE ROW LEVEL SECURITY;
ALTER TABLE amazon_finance_transaction_identifiers ENABLE ROW LEVEL SECURITY;
ALTER TABLE amazon_finance_transaction_identifiers FORCE ROW LEVEL SECURITY;
ALTER TABLE lingxing_profit_order_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE lingxing_profit_order_transactions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS amazon_finance_tenant_select ON amazon_finance_transactions;
CREATE POLICY amazon_finance_tenant_select ON amazon_finance_transactions
    FOR SELECT
    USING (tenant_id = current_setting('app.tenant_id', true));

DROP POLICY IF EXISTS amazon_finance_items_tenant_select ON amazon_finance_items;
CREATE POLICY amazon_finance_items_tenant_select ON amazon_finance_items
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
            FROM amazon_finance_transactions t
            WHERE t.seller_id = amazon_finance_items.seller_id
              AND t.transaction_id = amazon_finance_items.transaction_id
              AND t.tenant_id = current_setting('app.tenant_id', true)
        )
    );

DROP POLICY IF EXISTS amazon_finance_amount_lines_tenant_select ON amazon_finance_amount_lines;
CREATE POLICY amazon_finance_amount_lines_tenant_select ON amazon_finance_amount_lines
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
            FROM amazon_finance_transactions t
            WHERE t.seller_id = amazon_finance_amount_lines.seller_id
              AND t.transaction_id = amazon_finance_amount_lines.transaction_id
              AND t.tenant_id = current_setting('app.tenant_id', true)
        )
    );

DROP POLICY IF EXISTS amazon_finance_identifiers_tenant_select ON amazon_finance_transaction_identifiers;
CREATE POLICY amazon_finance_identifiers_tenant_select ON amazon_finance_transaction_identifiers
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
            FROM amazon_finance_transactions t
            WHERE t.seller_id = amazon_finance_transaction_identifiers.seller_id
              AND t.transaction_id = amazon_finance_transaction_identifiers.transaction_id
              AND t.tenant_id = current_setting('app.tenant_id', true)
        )
    );

DROP POLICY IF EXISTS lingxing_profit_tenant_select ON lingxing_profit_order_transactions;
CREATE POLICY lingxing_profit_tenant_select ON lingxing_profit_order_transactions
    FOR SELECT
    USING (tenant_id = current_setting('app.tenant_id', true));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'amazon_finance_reader') THEN
        REVOKE ALL ON amazon_finance_transactions FROM amazon_finance_reader;
        REVOKE ALL ON amazon_finance_items FROM amazon_finance_reader;
        REVOKE ALL ON amazon_finance_amount_lines FROM amazon_finance_reader;
        REVOKE ALL ON amazon_finance_transaction_identifiers FROM amazon_finance_reader;
        REVOKE ALL ON lingxing_profit_order_transactions FROM amazon_finance_reader;
        GRANT SELECT ON amazon_finance_released_transactions TO amazon_finance_reader;
        GRANT SELECT ON amazon_finance_released_items TO amazon_finance_reader;
        GRANT SELECT ON amazon_finance_released_amount_lines TO amazon_finance_reader;
        GRANT SELECT ON amazon_finance_released_identifiers TO amazon_finance_reader;
        GRANT SELECT ON lingxing_profit_report_query TO amazon_finance_reader;
    END IF;
END
$$;

COMMIT;
