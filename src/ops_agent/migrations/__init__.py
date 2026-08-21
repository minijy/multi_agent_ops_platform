from __future__ import annotations

from pathlib import Path


def main() -> int:
    from alembic import command
    from alembic.config import Config

    from ops_agent.config import Settings

    settings = Settings()
    settings.validate_runtime()
    root = Path.cwd()
    if not (root / "alembic.ini").is_file():
        root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "head")
    print("Alembic upgrade complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
