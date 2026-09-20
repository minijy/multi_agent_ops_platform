from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from ..config import Settings


LOGGER = logging.getLogger(__name__)


class SessionBusyError(RuntimeError):
    """Raised when another turn owns the same conversation lease."""


@dataclass
class SessionTurnLease:
    _release: Callable[[], None]
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._release()

    def __enter__(self) -> "SessionTurnLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


@dataclass
class _LocalLockEntry:
    lock: threading.Lock
    references: int = 0


class _LocalSessionBackend:
    """Single-process fallback used only when Redis is not configured."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, _LocalLockEntry] = {}
        self._results: dict[str, tuple[float, dict[str, Any]]] = {}

    def acquire(self, key: str, *, wait_seconds: float, ttl_seconds: int) -> SessionTurnLease:
        del ttl_seconds
        with self._guard:
            entry = self._locks.setdefault(key, _LocalLockEntry(lock=threading.Lock()))
            entry.references += 1
        acquired = entry.lock.acquire(timeout=wait_seconds)
        if not acquired:
            with self._guard:
                entry.references -= 1
                if entry.references == 0:
                    self._locks.pop(key, None)
            raise SessionBusyError("another request is already processing this session")

        def release() -> None:
            entry.lock.release()
            with self._guard:
                entry.references -= 1
                if entry.references == 0:
                    self._locks.pop(key, None)

        return SessionTurnLease(release)

    def get_result(self, key: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._guard:
            item = self._results.get(key)
            if item is None:
                return None
            expires_at, payload = item
            if expires_at <= now:
                self._results.pop(key, None)
                return None
            return dict(payload)

    def put_result(self, key: str, payload: dict[str, Any], *, ttl_seconds: int) -> None:
        with self._guard:
            self._results[key] = (time.monotonic() + ttl_seconds, dict(payload))

    def close(self) -> None:
        return None


class _RedisSessionBackend:
    def __init__(self, url: str) -> None:
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - exercised in deployed environment
            raise RuntimeError("redis package is required when SESSION_REDIS_URL is configured") from exc
        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._client.ping()

    def acquire(self, key: str, *, wait_seconds: float, ttl_seconds: int) -> SessionTurnLease:
        lock = self._client.lock(
            key,
            timeout=ttl_seconds,
            blocking_timeout=wait_seconds,
            thread_local=False,
        )
        if not lock.acquire(blocking=True):
            raise SessionBusyError("another request is already processing this session")
        stopped = threading.Event()

        def renew() -> None:
            interval = max(1.0, ttl_seconds / 3)
            while not stopped.wait(interval):
                try:
                    lock.extend(ttl_seconds, replace_ttl=True)
                except Exception:
                    LOGGER.exception(
                        "failed to renew Redis session lease", extra={"lock_key": key}
                    )
                    return

        renewal = threading.Thread(
            target=renew,
            daemon=True,
            name="session-lease-renewal",
        )
        renewal.start()

        def release() -> None:
            stopped.set()
            try:
                lock.release()
            except Exception:
                LOGGER.exception("failed to release Redis session lease", extra={"lock_key": key})

        return SessionTurnLease(release)

    def get_result(self, key: str) -> dict[str, Any] | None:
        raw = self._client.get(key)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            self._client.delete(key)
            return None
        return payload if isinstance(payload, dict) else None

    def put_result(self, key: str, payload: dict[str, Any], *, ttl_seconds: int) -> None:
        self._client.set(key, json.dumps(payload, ensure_ascii=False), ex=ttl_seconds)

    def close(self) -> None:
        self._client.close()


class SessionTurnCoordinator:
    """Serialize turns and deduplicate retries without storing conversation text."""

    def __init__(
        self,
        *,
        redis_url: str = "",
        lock_wait_seconds: float = 5.0,
        lock_ttl_seconds: int = 600,
        idempotency_ttl_seconds: int = 86_400,
    ) -> None:
        self.lock_wait_seconds = lock_wait_seconds
        self.lock_ttl_seconds = lock_ttl_seconds
        self.idempotency_ttl_seconds = idempotency_ttl_seconds
        self.backend_name = "redis" if redis_url.strip() else "local"
        self._backend = (
            _RedisSessionBackend(redis_url.strip()) if redis_url.strip() else _LocalSessionBackend()
        )

    @staticmethod
    def _digest(*parts: str) -> str:
        serialized = "\x1f".join(parts).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def stable_session_id(self, tenant_id: str, user_id: str, idempotency_key: str) -> str:
        identity = self._digest(tenant_id, user_id, idempotency_key)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ops-agent:{identity}"))

    def acquire(self, *, tenant_id: str, user_id: str, session_id: str) -> SessionTurnLease:
        digest = self._digest(tenant_id, user_id, session_id)
        return self._backend.acquire(
            f"ops:session:turn:{digest}",
            wait_seconds=self.lock_wait_seconds,
            ttl_seconds=self.lock_ttl_seconds,
        )

    def get_result(
        self, *, tenant_id: str, user_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        digest = self._digest(tenant_id, user_id, idempotency_key)
        return self._backend.get_result(f"ops:session:result:{digest}")

    def put_result(
        self,
        *,
        tenant_id: str,
        user_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        if not idempotency_key:
            return
        digest = self._digest(tenant_id, user_id, idempotency_key)
        self._backend.put_result(
            f"ops:session:result:{digest}",
            payload,
            ttl_seconds=self.idempotency_ttl_seconds,
        )

    def close(self) -> None:
        self._backend.close()


def create_session_turn_coordinator(settings: Settings) -> SessionTurnCoordinator:
    return SessionTurnCoordinator(
        redis_url=settings.session_redis_url,
        lock_wait_seconds=settings.session_lock_wait_seconds,
        lock_ttl_seconds=settings.session_lock_ttl_seconds,
        idempotency_ttl_seconds=settings.session_idempotency_ttl_seconds,
    )
