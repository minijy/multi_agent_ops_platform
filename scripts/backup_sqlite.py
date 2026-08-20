#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create consistent platform backups")
    parser.add_argument(
        "--postgres-dsn",
        default=os.getenv("POSTGRES_DSN", ""),
        help="PostgreSQL DSN; defaults to POSTGRES_DSN",
    )
    parser.add_argument("--skip-postgres", action="store_true", help="Back up SQLite files only")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "data"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = root / "backups" / stamp
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    if source.exists():
        for path in source.glob("*.sqlite3"):
            target = destination / path.name
            with sqlite3.connect(path) as source_db, sqlite3.connect(target) as target_db:
                source_db.backup(target_db)
            copied += 1
    print(f"copied {copied} sqlite files to {destination}")
    if args.skip_postgres:
        return 0
    if not args.postgres_dsn:
        print("PostgreSQL skipped: set POSTGRES_DSN or pass --postgres-dsn")
        return 0
    target = destination / "postgres.dump"
    try:
        subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(target), args.postgres_dsn],
            check=True,
        )
    except FileNotFoundError as error:
        raise SystemExit("pg_dump is required for PostgreSQL backup") from error
    print(f"created PostgreSQL custom-format backup at {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
