BEGIN;

CREATE TABLE IF NOT EXISTS amazon_finance_transactions (
    seller_id          text        NOT NULL,
    transaction_id     text        NOT NULL,
    marketplace_id     text        NOT NULL,
    account_type       text,
    transaction_status text        NOT NULL CHECK (transaction_status = 'RELEASED'),
    transaction_type   text        NOT NULL,
    description        text,
    posted_at          timestamptz NOT NULL,
    currency_code      char(3)     NOT NULL,
    total_amount       numeric(20, 6) NOT NULL,
    first_seen_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (seller_id, transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_amazon_finance_transactions_posted
    ON amazon_finance_transactions (seller_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_amazon_finance_transactions_type
    ON amazon_finance_transactions (seller_id, transaction_type, posted_at);
CREATE INDEX IF NOT EXISTS idx_amazon_finance_transactions_marketplace
    ON amazon_finance_transactions (seller_id, marketplace_id, posted_at);

CREATE TABLE IF NOT EXISTS amazon_finance_transaction_identifiers (
    seller_id       text NOT NULL,
    transaction_id  text NOT NULL,
    identifier_name text NOT NULL,
    identifier_value text NOT NULL,
    PRIMARY KEY (seller_id, transaction_id, identifier_name, identifier_value),
    FOREIGN KEY (seller_id, transaction_id)
        REFERENCES amazon_finance_transactions (seller_id, transaction_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_amazon_finance_identifiers_lookup
    ON amazon_finance_transaction_identifiers (identifier_name, identifier_value);

CREATE TABLE IF NOT EXISTS amazon_finance_items (
    id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    seller_id                text NOT NULL,
    transaction_id           text NOT NULL,
    item_index               integer NOT NULL,
    description              text,
    sku                      text,
    asin                     text,
    quantity_shipped         integer,
    fulfillment_network      text,
    currency_code            char(3),
    total_amount             numeric(20, 6),
    order_adjustment_item_id text,
    invoice_id               text,
    item_transaction_id      text,
    UNIQUE (seller_id, transaction_id, item_index),
    FOREIGN KEY (seller_id, transaction_id)
        REFERENCES amazon_finance_transactions (seller_id, transaction_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_amazon_finance_items_sku
    ON amazon_finance_items (seller_id, sku);
CREATE INDEX IF NOT EXISTS idx_amazon_finance_items_asin
    ON amazon_finance_items (seller_id, asin);

CREATE TABLE IF NOT EXISTS amazon_finance_amount_lines (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    seller_id          text NOT NULL,
    transaction_id     text NOT NULL,
    item_id            bigint,
    source_scope       text NOT NULL CHECK (source_scope IN ('ITEM', 'TRANSACTION')),
    line_index         integer NOT NULL,
    category_level_1   text,
    category_level_2   text,
    category_level_3   text,
    category_level_4   text,
    breakdown_type     text NOT NULL,
    currency_code      char(3) NOT NULL,
    amount             numeric(20, 6) NOT NULL,
    UNIQUE (seller_id, transaction_id, source_scope, item_id, line_index),
    FOREIGN KEY (seller_id, transaction_id)
        REFERENCES amazon_finance_transactions (seller_id, transaction_id)
        ON DELETE CASCADE,
    FOREIGN KEY (item_id)
        REFERENCES amazon_finance_items (id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_amazon_finance_amount_lines_posting
    ON amazon_finance_amount_lines (seller_id, breakdown_type);
CREATE INDEX IF NOT EXISTS idx_amazon_finance_amount_lines_transaction
    ON amazon_finance_amount_lines (seller_id, transaction_id);

COMMIT;
