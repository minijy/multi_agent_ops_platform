#!/usr/bin/env python3
"""Import generated mock analytics fixtures into PostgreSQL.

Generate files first with scripts/generate_mock_profit_data.py, or pass --generate.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from psycopg import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "mock_data"
AMAZON_SCHEMA = ROOT / "scripts" / "sql" / "amazon_finance_schema.sql"
LINGXING_DDL = ROOT / "scripts" / "sql" / "create_lingxing_profit_order_transactions.sql"


def resolve_dsn(explicit: str | None) -> str:
    dsn = explicit or os.environ.get("ANALYTICS_DSN") or os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise SystemExit(
            "Missing database URL. Set ANALYTICS_DSN in .env or pass --database-url."
        )
    return dsn


def reset_tables(dsn: str) -> None:
    statements = [
        "TRUNCATE lingxing_profit_order_transactions RESTART IDENTITY CASCADE;",
        "TRUNCATE amazon_finance_amount_lines RESTART IDENTITY CASCADE;",
        "TRUNCATE amazon_finance_items RESTART IDENTITY CASCADE;",
        "TRUNCATE amazon_finance_transaction_identifiers RESTART IDENTITY CASCADE;",
        "TRUNCATE amazon_finance_transactions RESTART IDENTITY CASCADE;",
    ]
    with connect(dsn) as connection:
        for statement in statements:
            try:
                connection.execute(statement)
            except Exception:
                pass


def import_amazon_pages(dsn: str) -> int:
    api_dir = FIXTURES / "api_pages"
    if not api_dir.is_dir():
        raise SystemExit(f"Missing fixture directory: {api_dir}")
    pages = sorted(api_dir.glob("*.json"))
    if not pages:
        raise SystemExit(f"No Amazon JSON pages found under {api_dir}")
    for page in pages:
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "import_amazon_finance.py"),
                str(page),
                "--database-url",
                dsn,
            ],
            check=True,
        )
    return len(pages)


def import_lingxing_xlsx(dsn: str, *, truncate: bool) -> int:
    xlsx_dir = FIXTURES / "xlsx"
    if not xlsx_dir.is_dir():
        raise SystemExit(f"Missing fixture directory: {xlsx_dir}")
    files = sorted(xlsx_dir.glob("*.xlsx"))
    if not files:
        raise SystemExit(f"No XLSX fixtures found under {xlsx_dir}")
    for index, path in enumerate(files):
        command = [
            sys.executable,
            str(ROOT / "scripts" / "import_lingxing_profit_xlsx.py"),
            str(path),
            "--dsn",
            dsn,
            "--ddl",
            str(LINGXING_DDL),
        ]
        if truncate and index == 0:
            command.append("--truncate")
        if index > 0:
            command.append("--skip-ddl")
        subprocess.run(command, check=True)
    return len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default="",
        help="PostgreSQL DSN (defaults to ANALYTICS_DSN or POSTGRES_DSN)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Truncate analytics tables before import",
    )
    parser.add_argument(
        "--skip-amazon",
        action="store_true",
        help="Skip Amazon finance JSON import",
    )
    parser.add_argument(
        "--skip-lingxing",
        action="store_true",
        help="Skip LingXing profit XLSX import",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Run generate_mock_profit_data.py before import",
    )
    args = parser.parse_args()

    dsn = resolve_dsn(args.database_url or None)
    if args.generate:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_mock_profit_data.py")],
            check=True,
        )
    if not FIXTURES.is_dir():
        raise SystemExit(
            f"Fixture root not found: {FIXTURES}\n"
            "Generate mock data first:\n"
            "  python scripts/generate_mock_profit_data.py\n"
            "Or run this script with --generate."
        )

    if args.reset:
        reset_tables(dsn)

    if not args.skip_amazon:
        if not AMAZON_SCHEMA.is_file():
            raise SystemExit(f"Missing schema file: {AMAZON_SCHEMA}")
        page_count = import_amazon_pages(dsn)
        print(f"Imported {page_count} Amazon finance JSON pages")
    else:
        page_count = 0

    if not args.skip_lingxing:
        xlsx_count = import_lingxing_xlsx(dsn, truncate=args.reset)
        print(f"Imported {xlsx_count} LingXing profit XLSX files")
    else:
        xlsx_count = 0

    print(
        "Mock analytics install complete:",
        f"amazon_pages={page_count}, lingxing_xlsx={xlsx_count}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
