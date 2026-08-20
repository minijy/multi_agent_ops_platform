from __future__ import annotations

import logging
import signal
import threading

from ..config import get_settings
from .stack import open_runtime_stack

LOGGER = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    stop = threading.Event()

    def request_stop(*_args) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    with open_runtime_stack(settings) as stack:
        service = stack.memory_service
        if service is None:
            raise RuntimeError("memory service is disabled")
        LOGGER.info("memory lifecycle worker started")
        while not stop.is_set():
            for tenant_id in service.control.tenant_ids():
                try:
                    service.maintenance(tenant_id)
                except Exception:
                    LOGGER.exception("memory maintenance failed tenant=%s", tenant_id)
            stop.wait(settings.memory_worker_poll_seconds)
        LOGGER.info("memory lifecycle worker stopped")


if __name__ == "__main__":
    main()
