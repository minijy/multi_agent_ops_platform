#!/usr/bin/env python3
"""Import RELEASED Amazon Finances listTransactions data into PostgreSQL."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "sql" / "amazon_finance_schema.sql"


def money(value: dict[str, Any] | None) -> tuple[str | None, str | None]:
    value = value or {}
    amount = value.get("currencyAmount")
    currency = value.get("currencyCode")
    return (None if amount is None else str(amount), currency)


def identifiers(values: Iterable[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name = value.get("itemRelatedIdentifierName") or value.get("relatedIdentifierName")
        identifier = value.get("itemRelatedIdentifierValue") or value.get(
            "relatedIdentifierValue"
        )
        if name and identifier:
            result[str(name)] = str(identifier)
    return result


def product_context(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for context in item.get("contexts") or []:
        if context.get("contextType") == "ProductContext":
            result.update(context)
    return result


def leaf_breakdowns(
    values: Iterable[dict[str, Any]], path: tuple[str, ...] = ()
) -> Iterable[tuple[tuple[str, ...], dict[str, Any]]]:
    for value in values or []:
        breakdown_type = str(value.get("breakdownType") or "")
        current_path = path + (breakdown_type,)
        children = value.get("breakdowns") or []
        if children:
            yield from leaf_breakdowns(children, current_path)
        elif breakdown_type:
            if len(current_path) > 4:
                raise ValueError(
                    f"Breakdown depth {len(current_path)} exceeds schema limit"
                )
            yield current_path, value


def write_csv(path: Path, columns: list[str], rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})
            count += 1
    return count


def psql_copy(path: Path, table: str, columns: list[str]) -> str:
    escaped_path = str(path).replace("'", "''")
    return f"\\copy {table} ({', '.join(columns)}) FROM '{escaped_path}' CSV HEADER;\n"


def run_psql(
    database_url: str, *, file: Path | None = None, sql: str | None = None
) -> None:
    command = ["psql", database_url, "-v", "ON_ERROR_STOP=1"]
    if file:
        command.extend(["-f", str(file)])
    subprocess.run(command, input=sql, text=True, check=True)


def import_json_file(json_file: Path, database_url: str) -> dict[str, int]:
    document = json.loads(json_file.read_text(encoding="utf-8"))
    payload = document.get("payload") or {}
    transactions = [
        transaction
        for transaction in payload.get("transactions") or []
        if transaction.get("transactionStatus") == "RELEASED"
    ]
    transaction_ids = [str(value["transactionId"]) for value in transactions]
    if len(transaction_ids) != len(set(transaction_ids)):
        raise ValueError("Input contains duplicate RELEASED transactionId values")

    transaction_rows: list[dict[str, Any]] = []
    identifier_rows: list[dict[str, Any]] = []
    item_rows: list[dict[str, Any]] = []
    amount_rows: list[dict[str, Any]] = []

    for transaction in transactions:
        metadata = transaction.get("sellingPartnerMetadata") or {}
        marketplace = transaction.get("marketplaceDetails") or {}
        seller_id = str(metadata["sellingPartnerId"])
        transaction_id = str(transaction["transactionId"])
        total_amount, currency_code = money(transaction.get("totalAmount"))
        marketplace_id = metadata.get("marketplaceId") or marketplace.get("marketplaceId")
        transaction_rows.append(
            {
                "seller_id": seller_id,
                "transaction_id": transaction_id,
                "marketplace_id": marketplace_id,
                "account_type": metadata.get("accountType"),
                "transaction_status": transaction["transactionStatus"],
                "transaction_type": transaction["transactionType"],
                "description": transaction.get("description"),
                "posted_at": transaction["postedDate"],
                "currency_code": currency_code,
                "total_amount": total_amount,
            }
        )
        for related in transaction.get("relatedIdentifiers") or []:
            if related.get("relatedIdentifierName") and related.get(
                "relatedIdentifierValue"
            ):
                identifier_rows.append(
                    {
                        "seller_id": seller_id,
                        "transaction_id": transaction_id,
                        "identifier_name": related["relatedIdentifierName"],
                        "identifier_value": related["relatedIdentifierValue"],
                    }
                )

        items = transaction.get("items") or []
        for item_index, item in enumerate(items):
            context = product_context(item)
            item_identifiers = identifiers(item.get("relatedIdentifiers") or [])
            item_amount, item_currency = money(item.get("totalAmount"))
            item_rows.append(
                {
                    "seller_id": seller_id,
                    "transaction_id": transaction_id,
                    "item_index": item_index,
                    "description": item.get("description"),
                    "sku": context.get("sku"),
                    "asin": context.get("asin"),
                    "quantity_shipped": context.get("quantityShipped"),
                    "fulfillment_network": context.get("fulfillmentNetwork"),
                    "currency_code": item_currency,
                    "total_amount": item_amount,
                    "order_adjustment_item_id": item_identifiers.get(
                        "ORDER_ADJUSTMENT_ITEM_ID"
                    ),
                    "invoice_id": item_identifiers.get("INVOICE_ID"),
                    "item_transaction_id": item_identifiers.get("TRANSACTION_ID"),
                }
            )
            for line_index, (path, breakdown) in enumerate(
                leaf_breakdowns(item.get("breakdowns") or [])
            ):
                amount, currency = money(breakdown.get("breakdownAmount"))
                amount_rows.append(
                    {
                        "seller_id": seller_id,
                        "transaction_id": transaction_id,
                        "source_scope": "ITEM",
                        "item_index": item_index,
                        "line_index": line_index,
                        "category_level_1": path[0] if len(path) > 0 else None,
                        "category_level_2": path[1] if len(path) > 1 else None,
                        "category_level_3": path[2] if len(path) > 2 else None,
                        "category_level_4": path[3] if len(path) > 3 else None,
                        "breakdown_type": path[-1],
                        "currency_code": currency,
                        "amount": amount,
                    }
                )

        if not items:
            for line_index, (path, breakdown) in enumerate(
                leaf_breakdowns(transaction.get("breakdowns") or [])
            ):
                amount, currency = money(breakdown.get("breakdownAmount"))
                amount_rows.append(
                    {
                        "seller_id": seller_id,
                        "transaction_id": transaction_id,
                        "source_scope": "TRANSACTION",
                        "item_index": None,
                        "line_index": line_index,
                        "category_level_1": path[0] if len(path) > 0 else None,
                        "category_level_2": path[1] if len(path) > 1 else None,
                        "category_level_3": path[2] if len(path) > 2 else None,
                        "category_level_4": path[3] if len(path) > 3 else None,
                        "breakdown_type": path[-1],
                        "currency_code": currency,
                        "amount": amount,
                    }
                )

    run_psql(database_url, file=SCHEMA_PATH)

    transaction_columns = [
        "seller_id",
        "transaction_id",
        "marketplace_id",
        "account_type",
        "transaction_status",
        "transaction_type",
        "description",
        "posted_at",
        "currency_code",
        "total_amount",
    ]
    identifier_columns = [
        "seller_id",
        "transaction_id",
        "identifier_name",
        "identifier_value",
    ]
    item_columns = [
        "seller_id",
        "transaction_id",
        "item_index",
        "description",
        "sku",
        "asin",
        "quantity_shipped",
        "fulfillment_network",
        "currency_code",
        "total_amount",
        "order_adjustment_item_id",
        "invoice_id",
        "item_transaction_id",
    ]
    amount_columns = [
        "seller_id",
        "transaction_id",
        "source_scope",
        "item_index",
        "line_index",
        "category_level_1",
        "category_level_2",
        "category_level_3",
        "category_level_4",
        "breakdown_type",
        "currency_code",
        "amount",
    ]

    with tempfile.TemporaryDirectory(prefix="amazon-finance-") as temporary:
        directory = Path(temporary)
        transaction_file = directory / "transactions.csv"
        identifier_file = directory / "identifiers.csv"
        item_file = directory / "items.csv"
        amount_file = directory / "amounts.csv"
        counts = {
            "transactions": write_csv(
                transaction_file, transaction_columns, transaction_rows
            ),
            "identifiers": write_csv(
                identifier_file, identifier_columns, identifier_rows
            ),
            "items": write_csv(item_file, item_columns, item_rows),
            "amount_lines": write_csv(amount_file, amount_columns, amount_rows),
        }

        sql = """
BEGIN;
CREATE TEMP TABLE stg_transactions
    (LIKE amazon_finance_transactions INCLUDING DEFAULTS);
CREATE TEMP TABLE stg_identifiers
    (LIKE amazon_finance_transaction_identifiers);
CREATE TEMP TABLE stg_items (
    seller_id text, transaction_id text, item_index integer, description text,
    sku text, asin text, quantity_shipped integer, fulfillment_network text,
    currency_code char(3), total_amount numeric(20,6),
    order_adjustment_item_id text, invoice_id text, item_transaction_id text
);
CREATE TEMP TABLE stg_amounts (
    seller_id text, transaction_id text, source_scope text, item_index integer,
    line_index integer, category_level_1 text, category_level_2 text,
    category_level_3 text, category_level_4 text, breakdown_type text,
    currency_code char(3), amount numeric(20,6)
);
"""
        sql += psql_copy(transaction_file, "stg_transactions", transaction_columns)
        sql += psql_copy(identifier_file, "stg_identifiers", identifier_columns)
        sql += psql_copy(item_file, "stg_items", item_columns)
        sql += psql_copy(amount_file, "stg_amounts", amount_columns)
        sql += """
INSERT INTO amazon_finance_transactions (
    seller_id, transaction_id, marketplace_id, account_type, transaction_status,
    transaction_type, description, posted_at, currency_code, total_amount
)
SELECT seller_id, transaction_id, marketplace_id, account_type, transaction_status,
       transaction_type, description, posted_at, currency_code, total_amount
FROM stg_transactions
ON CONFLICT (seller_id, transaction_id) DO UPDATE SET
    marketplace_id = EXCLUDED.marketplace_id,
    account_type = EXCLUDED.account_type,
    transaction_status = EXCLUDED.transaction_status,
    transaction_type = EXCLUDED.transaction_type,
    description = EXCLUDED.description,
    posted_at = EXCLUDED.posted_at,
    currency_code = EXCLUDED.currency_code,
    total_amount = EXCLUDED.total_amount,
    last_seen_at = now();

DELETE FROM amazon_finance_transaction_identifiers old
USING stg_transactions current
WHERE old.seller_id = current.seller_id
  AND old.transaction_id = current.transaction_id;
DELETE FROM amazon_finance_amount_lines old
USING stg_transactions current
WHERE old.seller_id = current.seller_id
  AND old.transaction_id = current.transaction_id;
DELETE FROM amazon_finance_items old
USING stg_transactions current
WHERE old.seller_id = current.seller_id
  AND old.transaction_id = current.transaction_id;

INSERT INTO amazon_finance_transaction_identifiers
SELECT * FROM stg_identifiers
ON CONFLICT DO NOTHING;

INSERT INTO amazon_finance_items (
    seller_id, transaction_id, item_index, description, sku, asin,
    quantity_shipped, fulfillment_network, currency_code, total_amount,
    order_adjustment_item_id, invoice_id, item_transaction_id
)
SELECT * FROM stg_items;

INSERT INTO amazon_finance_amount_lines (
    seller_id, transaction_id, item_id, source_scope, line_index,
    category_level_1, category_level_2, category_level_3, category_level_4,
    breakdown_type, currency_code, amount
)
SELECT amounts.seller_id, amounts.transaction_id, items.id, amounts.source_scope,
       amounts.line_index, amounts.category_level_1, amounts.category_level_2,
       amounts.category_level_3, amounts.category_level_4,
       amounts.breakdown_type, amounts.currency_code, amounts.amount
FROM stg_amounts amounts
LEFT JOIN amazon_finance_items items
  ON items.seller_id = amounts.seller_id
 AND items.transaction_id = amounts.transaction_id
 AND items.item_index = amounts.item_index;
COMMIT;
"""
        run_psql(database_url, sql=sql)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("json_file", type=Path)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("ANALYTICS_DSN")
        or os.environ.get("POSTGRES_DSN")
        or "",
    )
    args = parser.parse_args()
    if not args.database_url:
        print("Set ANALYTICS_DSN or pass --database-url", file=sys.stderr)
        return 1
    counts = import_json_file(args.json_file, args.database_url)
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
