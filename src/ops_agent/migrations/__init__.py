from __future__ import annotations

from pathlib import Path


def main() -> int:
    from alembic import command
    from alembic.config import Config

    from ops_agent.config import Settings

    settings = Settings()
    postgres_backends = {
        settings.control_plane_backend,
        settings.session_event_backend,
        settings.memory_backend,
    }
    if "postgres" not in postgres_backends:
        print("SQLite backends still use CREATE TABLE IF NOT EXISTS; Alembic is for PostgreSQL.")
        return 0
    root = Path.cwd()
    if not (root / "alembic.ini").is_file():
        root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "head")
    print("Alembic upgrade complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
