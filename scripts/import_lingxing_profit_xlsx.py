#!/usr/bin/env python3
"""Import LingXing profit report (order transaction) XLSX into PostgreSQL."""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from psycopg import connect


COLUMN_MAP = [
    ("store_name", "店铺"),
    ("country", "国家"),
    ("currency_code", "币种"),
    ("order_datetime", "下单时间"),
    ("payment_datetime", "付款时间"),
    ("shipment_datetime", "发货时间"),
    ("posted_datetime", "结算时间"),
    ("fund_transfer_datetime", "转账时间"),
    ("deferred_datetime", "延迟时间"),
    ("order_id", "订单号"),
    ("settlement_id", "Settlement Id"),
    ("settlement_fid", "结算编号"),
    ("msku", "MSKU"),
    ("fnsku", "FNSKU"),
    ("asin", "ASIN"),
    ("parent_asin", "父asin"),
    ("local_name", "品名"),
    ("local_sku", "SKU"),
    ("account_type", "账单类型"),
    ("event_source", "费用类型"),
    ("fulfillment", "订单类型"),
    ("description", "描述"),
    ("settlement_status", "结算状态"),
    ("fund_transfer_status", "转账状态"),
    ("transaction_status", "交易状态"),
    ("quantity", "数量"),
    ("principal_realname", "Listing负责人"),
    ("product_developer_realname", "开发负责人"),
    ("product_sales", "销售额"),
    ("product_sales_tax", "销售税"),
    ("shipping_credits", "买家运费"),
    ("shipping_credits_tax", "买家运费税"),
    ("giftwrap_credits", "礼品包装"),
    ("giftwrap_credits_tax", "礼品包装税"),
    ("amazon_point_fee", "积分"),
    ("promotional_rebates", "促销折扣"),
    ("promotional_rebates_tax", "促销折扣税"),
    ("sales_tax_collected", "代扣代缴增值税"),
    ("low_value_goods", "低价值商品税"),
    ("marketplace_withheld_tax", "市场预扣税"),
    ("tcs_cgst", "TCS_CGST"),
    ("tcs_sgst", "TCS_SGST"),
    ("tcs_igst", "TCS_IGST"),
    ("selling_fees", "平台费"),
    ("fba_fees", "FBA费"),
    ("other_transaction_fees", "其他交易费"),
    ("other_amount", "其他"),
    ("hidden_tax", "隐藏税"),
    ("settlement_total", "亚马逊结算小计"),
    ("purchase_costs_total", "采购成本"),
    ("logistics_costs_total", "头程费用"),
    ("other_costs_total", "其他成本"),
    ("custom_order_fee_total", "站外推广费"),
    ("settlement_gross_profit", "结算订单毛利润"),
    ("settlement_gross_profit_rate", "结算订单毛利润率"),
    ("promotion_id", "促销编码"),
]

NUMERIC_FIELDS = {
    "quantity",
    "product_sales",
    "product_sales_tax",
    "shipping_credits",
    "shipping_credits_tax",
    "giftwrap_credits",
    "giftwrap_credits_tax",
    "amazon_point_fee",
    "promotional_rebates",
    "promotional_rebates_tax",
    "sales_tax_collected",
    "low_value_goods",
    "marketplace_withheld_tax",
    "tcs_cgst",
    "tcs_sgst",
    "tcs_igst",
    "selling_fees",
    "fba_fees",
    "other_transaction_fees",
    "other_amount",
    "hidden_tax",
    "settlement_total",
    "purchase_costs_total",
    "logistics_costs_total",
    "other_costs_total",
    "custom_order_fee_total",
    "settlement_gross_profit",
    "settlement_gross_profit_rate",
}

DATETIME_FIELDS = {
    "order_datetime",
    "payment_datetime",
    "shipment_datetime",
    "posted_datetime",
    "fund_transfer_datetime",
    "deferred_datetime",
}


def _col_row(ref: str) -> tuple[int, int]:
    col = ""
    row = ""
    for ch in ref:
        if ch.isalpha():
            col += ch
        else:
            row += ch
    index = 0
    for ch in col:
        index = index * 26 + (ord(ch.upper()) - 64)
    return index - 1, int(row) - 1


def read_xlsx_rows(path: Path) -> tuple[list[str], list[list[object | None]]]:
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for item in root.findall("m:si", ns):
                texts = [node.text or "" for node in item.findall(".//m:t", ns)]
                shared.append("".join(texts))
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

        def cell_value(cell: ET.Element) -> object | None:
            cell_type = cell.get("t")
            if cell_type == "inlineStr":
                texts = [node.text or "" for node in cell.findall(".//m:t", ns)]
                joined = "".join(texts)
                return joined if joined else None
            value_node = cell.find("m:v", ns)
            if value_node is None:
                return None
            raw = value_node.text
            if cell_type == "s":
                return shared[int(raw or 0)]
            if raw is None:
                return None
            if re.fullmatch(r"-?\d+(\.\d+)?", raw):
                if "." in raw:
                    return float(raw)
                return int(raw)
            return raw

        rows_map: dict[int, dict[int, object | None]] = {}
        max_col = 0
        for cell in sheet.findall(".//m:sheetData/m:row/m:c", ns):
            ref = cell.get("r")
            if not ref:
                continue
            col, row = _col_row(ref)
            rows_map.setdefault(row, {})[col] = cell_value(cell)
            max_col = max(max_col, col)
        table = [
            [rows_map.get(row_index, {}).get(col_index) for col_index in range(max_col + 1)]
            for row_index in range(max(rows_map) + 1)
        ]
    header_row = 1
    header = [str(value).strip() if value is not None else "" for value in table[header_row]]
    data_rows = table[header_row + 1 :]
    return header, data_rows


def parse_datetime(value: object | None) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_decimal(value: object | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def normalize_record(
    header: list[str],
    row: list[object | None],
    *,
    source_file: str,
    source_row: int,
) -> dict[str, object | None]:
    index_by_header = {name: idx for idx, name in enumerate(header)}
    record: dict[str, object | None] = {
        "source_file": source_file,
        "source_row": source_row,
    }
    for field, label in COLUMN_MAP:
        idx = index_by_header.get(label)
        raw = row[idx] if idx is not None and idx < len(row) else None
        if field in DATETIME_FIELDS:
            record[field] = parse_datetime(raw)
        elif field in NUMERIC_FIELDS:
            record[field] = parse_decimal(raw)
        else:
            record[field] = None if raw in (None, "") else str(raw).strip()
    return record


def ensure_table(connection_dsn: str, ddl_path: Path) -> None:
    ddl = ddl_path.read_text(encoding="utf-8")
    with connect(connection_dsn) as connection:
        connection.execute(ddl)


def import_file(
    connection_dsn: str,
    xlsx_path: Path,
    *,
    truncate: bool,
    batch_size: int,
) -> int:
    header, rows = read_xlsx_rows(xlsx_path)
    records = [
        normalize_record(
            header,
            row,
            source_file=xlsx_path.name,
            source_row=index + 2,
        )
        for index, row in enumerate(rows)
        if any(cell not in (None, "") for cell in row)
    ]
    columns = ["source_file", "source_row", *[field for field, _ in COLUMN_MAP]]
    placeholders = ", ".join(f"%({column})s" for column in columns)
    statement = (
        "INSERT INTO lingxing_profit_order_transactions ("
        + ", ".join(columns)
        + f") VALUES ({placeholders})"
    )
    inserted = 0
    with connect(connection_dsn) as connection:
        with connection.transaction():
            if truncate:
                connection.execute("TRUNCATE lingxing_profit_order_transactions RESTART IDENTITY")
            for start in range(0, len(records), batch_size):
                batch = records[start : start + batch_size]
                with connection.cursor() as cursor:
                    cursor.executemany(statement, batch)
                inserted += len(batch)
    return inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", type=Path, help="领星利润报表 XLSX 文件路径")
    parser.add_argument(
        "--dsn",
        default="",
        help="PostgreSQL DSN，默认读取环境变量 ANALYTICS_DSN 或 POSTGRES_DSN",
    )
    parser.add_argument(
        "--ddl",
        type=Path,
        default=Path(__file__).resolve().parent / "sql" / "create_lingxing_profit_order_transactions.sql",
        help="建表 SQL 路径",
    )
    parser.add_argument("--truncate", action="store_true", help="导入前清空表")
    parser.add_argument("--skip-ddl", action="store_true", help="跳过建表")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    import os

    dsn = args.dsn or os.environ.get("ANALYTICS_DSN") or os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("missing DSN: pass --dsn or set ANALYTICS_DSN/POSTGRES_DSN", file=sys.stderr)
        return 2
    if not args.xlsx.is_file():
        print(f"file not found: {args.xlsx}", file=sys.stderr)
        return 2

    if not args.skip_ddl:
        ensure_table(dsn, args.ddl)
    count = import_file(dsn, args.xlsx, truncate=args.truncate, batch_size=args.batch_size)
    print(f"imported {count} rows from {args.xlsx.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
