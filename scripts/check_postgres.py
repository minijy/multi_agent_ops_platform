"""Initialize and verify the configured PostgreSQL persistence backends."""

from ops_agent.config import Settings
from ops_agent.infrastructure.platform_store import create_platform_store
from ops_agent.runtime.session_events import create_session_event_store


def main() -> None:
    settings = Settings()
    settings.validate_runtime()
    backends = {
        "control_plane": settings.control_plane_backend,
        "session_events": settings.session_event_backend,
    }
    if set(backends.values()) != {"postgres"}:
        raise SystemExit(f"All persistence backends must be postgres: {backends}")

    create_platform_store(settings)
    create_session_event_store(settings)
    print("PostgreSQL persistence connection and schema setup succeeded.")
    print(f"Backends: {backends}")


if __name__ == "__main__":
    main()
