#!/usr/bin/env python3
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    source = root / "data"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = root / "backups" / stamp
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    if source.exists():
        for path in source.glob("*.sqlite3"):
            shutil.copy2(path, destination / path.name)
            copied += 1
    print(f"copied {copied} sqlite files to {destination}")
    print("PostgreSQL: pg_dump \"$POSTGRES_DSN\" > backups/postgres.dump")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
