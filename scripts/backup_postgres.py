#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a PostgreSQL custom-format backup")
    parser.add_argument(
        "--postgres-dsn",
        default=os.getenv("POSTGRES_DSN", ""),
        help="PostgreSQL DSN; defaults to POSTGRES_DSN",
    )
    args = parser.parse_args()
    if not args.postgres_dsn:
        raise SystemExit("set POSTGRES_DSN or pass --postgres-dsn")
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = root / "backups" / stamp
    destination.mkdir(parents=True, exist_ok=True)
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
