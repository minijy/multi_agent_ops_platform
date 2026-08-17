#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fastapi.testclient import TestClient

from ops_agent.api.app import create_app
from ops_agent.config import Settings


def main() -> int:
    settings = Settings(
        _env_file=None,
        model_provider="mock",
        platform_db_path=Path("data/load-platform.sqlite3"),
        session_event_path=Path("data/load-events.sqlite3"),
        runtime_metrics_path=Path("data/load-metrics.sqlite3"),
    )
    with TestClient(create_app(settings)) as client:
        def once(index: int) -> int:
            response = client.post(
                "/v1/agent/query",
                json={"question": f"你好，请介绍当前 Runtime {index}"},
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(once, index) for index in range(16)]
            codes = [future.result() for future in as_completed(futures)]
    failed = [code for code in codes if code != 200]
    print(f"completed={len(codes)} failed={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
