"""Initialize and verify PostgreSQL persistence."""

from alembic.config import Config
from alembic.script import ScriptDirectory

from ops_agent.config import Settings
from ops_agent.infrastructure.platform_store import create_platform_store
from ops_agent.runtime.memory import PostgresMemoryStore
from ops_agent.runtime.session_events import create_session_event_store


def main() -> None:
    settings = Settings()
    settings.validate_runtime()

    create_platform_store(settings)
    create_session_event_store(settings)
    if settings.memory_enabled:
        PostgresMemoryStore(settings.postgres_dsn)

    import psycopg

    expected_revision = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    with psycopg.connect(settings.postgres_dsn) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    actual_revision = str(row[0]) if row else ""
    if actual_revision != expected_revision:
        raise SystemExit(
            f"Database migration is stale: actual={actual_revision!r}, "
            f"expected={expected_revision!r}. Run ops-agent-migrate."
        )
    print("PostgreSQL persistence and migration verification succeeded.")
    print(f"revision: {actual_revision}")


if __name__ == "__main__":
    main()
