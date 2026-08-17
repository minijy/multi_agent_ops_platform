from __future__ import annotations

import logging
import signal
import socket
import threading
import time
import uuid
from dataclasses import dataclass

from ..config import Settings, get_settings
from .governance import RuntimeGovernanceStore, SubagentTaskRecord
from .stack import RuntimeStack, open_runtime_stack
from .subagents import execute_subagent_task

logger = logging.getLogger(__name__)


def default_worker_id(settings: Settings) -> str:
    if settings.subagent_worker_id.strip():
        return settings.subagent_worker_id.strip()
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


@dataclass
class SubagentQueueWorker:
    stack: RuntimeStack
    worker_id: str
    stop_event: threading.Event

    @property
    def store(self) -> RuntimeGovernanceStore:
        return self.stack.governance_store

    @property
    def settings(self) -> Settings:
        return self.stack.settings

    def run_forever(self) -> None:
        logger.info(
            "subagent worker started id=%s backend=%s",
            self.worker_id,
            self.settings.session_event_backend,
        )
        while not self.stop_event.is_set():
            try:
                recovered = self.store.requeue_expired_leases(
                    max_attempts=self.settings.subagent_max_attempts
                )
                if recovered:
                    logger.info("requeued/expired %s subagent lease(s)", recovered)
                claimed = self.store.claim_next_task(
                    worker_id=self.worker_id,
                    lease_seconds=self.settings.subagent_lease_seconds,
                )
                if claimed is None:
                    self.stop_event.wait(self.settings.subagent_worker_poll_seconds)
                    continue
                self._process(claimed)
            except Exception:
                logger.exception("subagent worker loop failed")
                self.stop_event.wait(self.settings.subagent_worker_poll_seconds)
        logger.info("subagent worker stopped id=%s", self.worker_id)

    def _process(self, record: SubagentTaskRecord) -> None:
        cancellation = threading.Event()
        stop_heartbeat = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(self.settings.subagent_lease_renew_seconds):
                latest = self.store.renew_lease(
                    task_id=record.task_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.settings.subagent_lease_seconds,
                )
                if latest is None:
                    cancellation.set()
                    return
                if latest.status == "cancel_requested":
                    cancellation.set()

        thread = threading.Thread(
            target=heartbeat,
            name=f"subagent-lease-{record.task_id[:8]}",
            daemon=True,
        )
        thread.start()
        try:
            current = self.store.get_task(record.task_id, record.tenant_id) or record
            if current.status == "cancel_requested":
                cancellation.set()
            logger.info(
                "claimed subagent task=%s attempt=%s tenant=%s",
                record.task_id,
                record.attempt,
                record.tenant_id,
            )
            execute_subagent_task(
                runtime=self.stack.agent_runtime,
                store=self.store,
                event_store=self.stack.session_events,
                record=current,
                cancellation=cancellation,
            )
        finally:
            stop_heartbeat.set()
            thread.join(timeout=1)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    if settings.subagent_queue_backend != "db":
        logger.warning(
            "SUBAGENT_QUEUE_BACKEND=%s; worker will still claim from DB queue. "
            "Set SUBAGENT_QUEUE_BACKEND=db on the API so it only enqueues.",
            settings.subagent_queue_backend,
        )
    stop_event = threading.Event()

    def _stop(*_args: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    with open_runtime_stack(settings) as stack:
        # External workers must never start an inline pool that races the queue.
        stack.subagent_manager.shutdown()
        worker = SubagentQueueWorker(
            stack=stack,
            worker_id=default_worker_id(settings),
            stop_event=stop_event,
        )
        worker.run_forever()


if __name__ == "__main__":
    main()
