from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Illustrative USD per 1M tokens. Used for local cost dashboards, not billing.
_INPUT_OUTPUT_RATES: dict[tuple[str, str], tuple[float, float]] = {
    ("mock", "*"): (0.0, 0.0),
    ("fake", "*"): (0.0, 0.0),
    ("zhipu", "glm-4-flash"): (0.06, 0.06),
    ("zhipu", "glm-4.6v-flash"): (0.20, 0.20),
    ("zhipu", "glm-4.7-flashx"): (0.20, 0.20),
    ("zhipu", "glm-5.2"): (1.00, 4.00),
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("openai", "gpt-5.6-sol"): (1.25, 10.00),
    ("qwen", "qwen3.7-plus"): (0.80, 4.80),
    ("qwen", "qwen3-vl-235b-a22b-thinking"): (1.50, 12.00),
    ("qwen", "*"): (0.80, 4.80),
    ("deepseek", "deepseek-chat"): (0.28, 0.42),
    ("deepseek", "deepseek-reasoner"): (0.28, 0.42),
    ("deepseek", "deepseek-v4-flash"): (0.14, 0.28),
    ("deepseek", "deepseek-v4-pro"): (1.25, 2.50),
    ("deepseek", "*"): (0.28, 0.42),
}


class TurnMetric(BaseModel):
    metric_id: str
    session_id: str
    tenant_id: str
    user_id: str
    provider: str = ""
    model: str = ""
    status: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    error_code: str = ""
    created_at: str


DAILY_WINDOW_DAYS = 14
FAILED_STATUSES = ("failed", "timed_out", "cancelled", "budget_exceeded")


class DailyRuntimePoint(BaseModel):
    date: str
    turns: int = 0
    failed: int = 0
    tokens: int = 0
    avg_latency_ms: float = 0.0


class RuntimeMetricsSummary(BaseModel):
    turn_count: int = 0
    failed_turns: int = 0
    failure_rate: float = 0.0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    by_model: dict[str, int] = Field(default_factory=dict)
    daily: list[DailyRuntimePoint] = Field(default_factory=list)


def daily_window_start(days: int = DAILY_WINDOW_DAYS) -> date:
    return datetime.now(timezone.utc).date() - timedelta(days=days - 1)


def fill_daily_points(
    rows: list[Any],
    *,
    days: int = DAILY_WINDOW_DAYS,
) -> list[DailyRuntimePoint]:
    by_day: dict[str, Any] = {}
    for row in rows:
        key = str(row["day"] if row["day"] is not None else "")[:10]
        if key:
            by_day[key] = row
    start = daily_window_start(days)
    points: list[DailyRuntimePoint] = []
    for offset in range(days):
        key = (start + timedelta(days=offset)).isoformat()
        row = by_day.get(key)
        points.append(
            DailyRuntimePoint(
                date=key,
                turns=int(row["turns"] if row else 0),
                failed=int(row["failed"] if row else 0),
                tokens=int(row["tokens"] if row else 0),
                avg_latency_ms=round(float(row["avg_latency_ms"] if row else 0), 3),
            )
        )
    return points


def runtime_summary_from_aggregates(
    *,
    by_status: dict[str, int],
    by_model: dict[str, int],
    totals: Any,
    daily_rows: list[Any],
) -> RuntimeMetricsSummary:
    failed = sum(by_status.get(name, 0) for name in FAILED_STATUSES)
    turn_count = int(totals["turn_count"] or 0)
    return RuntimeMetricsSummary(
        turn_count=turn_count,
        failed_turns=failed,
        failure_rate=round(failed / turn_count, 4) if turn_count else 0.0,
        total_tokens=int(totals["total_tokens"] or 0),
        estimated_cost_usd=round(float(totals["estimated_cost_usd"] or 0), 8),
        avg_latency_ms=round(float(totals["avg_latency_ms"] or 0), 3),
        tool_calls=int(totals["tool_calls"] or 0),
        tool_errors=int(totals["tool_errors"] or 0),
        by_status=by_status,
        by_model=by_model,
        daily=fill_daily_points(daily_rows),
    )


class MetricsStore(Protocol):
    def record(self, metric: TurnMetric) -> None: ...

    def summarize(self, tenant_id: str) -> RuntimeMetricsSummary: ...


def estimate_cost(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    rates = _INPUT_OUTPUT_RATES.get((provider, model))
    if rates is None:
        rates = _INPUT_OUTPUT_RATES.get((provider, "*"), (0.0, 0.0))
    input_rate, output_rate = rates
    return round(
        (prompt_tokens / 1_000_000) * input_rate
        + (completion_tokens / 1_000_000) * output_rate,
        8,
    )


def usage_from_events(events: list[Any]) -> dict[str, Any]:
    prompt = 0
    completion = 0
    total = 0
    tool_calls = 0
    tool_errors = 0
    provider = ""
    model = ""
    for event in events:
        event_type = getattr(event, "event_type", "")
        payload = getattr(event, "payload", {}) or {}
        if event_type == "model.response":
            provider = str(payload.get("provider") or provider)
            model = str(payload.get("model") or model)
            usage = payload.get("usage") or {}
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
            total += int(usage.get("total_tokens") or 0)
        elif event_type == "turn.completed":
            total = max(total, int(payload.get("tokens_used") or 0))
        elif event_type == "tool.completed":
            tool_calls += 1
            if not payload.get("ok", True):
                tool_errors += 1
    if total == 0:
        total = prompt + completion
    return {
        "provider": provider,
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "estimated_cost_usd": estimate_cost(provider, model, prompt, completion),
    }


class SQLiteMetricsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_runtime_metrics(
                    metric_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    tool_errors INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runtime_metrics_tenant
                    ON agent_runtime_metrics(tenant_id, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, metric: TurnMetric) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runtime_metrics(
                    metric_id, session_id, tenant_id, user_id, provider, model,
                    status, prompt_tokens, completion_tokens, total_tokens,
                    estimated_cost_usd, latency_ms, tool_calls, tool_errors,
                    error_code, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    metric.metric_id,
                    metric.session_id,
                    metric.tenant_id,
                    metric.user_id,
                    metric.provider,
                    metric.model,
                    metric.status,
                    metric.prompt_tokens,
                    metric.completion_tokens,
                    metric.total_tokens,
                    metric.estimated_cost_usd,
                    metric.latency_ms,
                    metric.tool_calls,
                    metric.tool_errors,
                    metric.error_code,
                    metric.created_at,
                ),
            )

    def summarize(self, tenant_id: str) -> RuntimeMetricsSummary:
        since = daily_window_start().isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS amount FROM agent_runtime_metrics
                WHERE tenant_id=? GROUP BY status
                """,
                (tenant_id,),
            ).fetchall()
            models = connection.execute(
                """
                SELECT model, COUNT(*) AS amount FROM agent_runtime_metrics
                WHERE tenant_id=? AND model != '' GROUP BY model
                """,
                (tenant_id,),
            ).fetchall()
            totals = connection.execute(
                """
                SELECT
                    COUNT(*) AS turn_count,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                    COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                    COALESCE(SUM(tool_calls), 0) AS tool_calls,
                    COALESCE(SUM(tool_errors), 0) AS tool_errors
                FROM agent_runtime_metrics WHERE tenant_id=?
                """,
                (tenant_id,),
            ).fetchone()
            daily_rows = connection.execute(
                """
                SELECT
                    substr(created_at, 1, 10) AS day,
                    COUNT(*) AS turns,
                    COALESCE(SUM(CASE WHEN status IN ('failed','timed_out','cancelled','budget_exceeded') THEN 1 ELSE 0 END), 0) AS failed,
                    COALESCE(SUM(total_tokens), 0) AS tokens,
                    COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                FROM agent_runtime_metrics
                WHERE tenant_id=? AND substr(created_at, 1, 10) >= ?
                GROUP BY substr(created_at, 1, 10)
                ORDER BY 1
                """,
                (tenant_id, since),
            ).fetchall()
        return runtime_summary_from_aggregates(
            by_status={row["status"]: int(row["amount"]) for row in rows},
            by_model={row["model"]: int(row["amount"]) for row in models},
            totals=totals,
            daily_rows=daily_rows,
        )


class PostgresMetricsStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runtime_metrics(
                    metric_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt_tokens BIGINT NOT NULL DEFAULT 0,
                    completion_tokens BIGINT NOT NULL DEFAULT 0,
                    total_tokens BIGINT NOT NULL DEFAULT 0,
                    estimated_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
                    latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    tool_errors INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_runtime_metrics_tenant
                ON agent_runtime_metrics(tenant_id, created_at)
                """
            )

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    def record(self, metric: TurnMetric) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runtime_metrics(
                    metric_id, session_id, tenant_id, user_id, provider, model,
                    status, prompt_tokens, completion_tokens, total_tokens,
                    estimated_cost_usd, latency_ms, tool_calls, tool_errors,
                    error_code, created_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    metric.metric_id,
                    metric.session_id,
                    metric.tenant_id,
                    metric.user_id,
                    metric.provider,
                    metric.model,
                    metric.status,
                    metric.prompt_tokens,
                    metric.completion_tokens,
                    metric.total_tokens,
                    metric.estimated_cost_usd,
                    metric.latency_ms,
                    metric.tool_calls,
                    metric.tool_errors,
                    metric.error_code,
                    metric.created_at,
                ),
            )

    def summarize(self, tenant_id: str) -> RuntimeMetricsSummary:
        since = datetime.combine(
            daily_window_start(), datetime.min.time(), tzinfo=timezone.utc
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS amount FROM agent_runtime_metrics
                WHERE tenant_id=%s GROUP BY status
                """,
                (tenant_id,),
            ).fetchall()
            models = connection.execute(
                """
                SELECT model, COUNT(*) AS amount FROM agent_runtime_metrics
                WHERE tenant_id=%s AND model != '' GROUP BY model
                """,
                (tenant_id,),
            ).fetchall()
            totals = connection.execute(
                """
                SELECT
                    COUNT(*) AS turn_count,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                    COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                    COALESCE(SUM(tool_calls), 0) AS tool_calls,
                    COALESCE(SUM(tool_errors), 0) AS tool_errors
                FROM agent_runtime_metrics WHERE tenant_id=%s
                """,
                (tenant_id,),
            ).fetchone()
            daily_rows = connection.execute(
                """
                SELECT
                    to_char((created_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD') AS day,
                    COUNT(*) AS turns,
                    COALESCE(SUM(CASE WHEN status IN ('failed','timed_out','cancelled','budget_exceeded') THEN 1 ELSE 0 END), 0) AS failed,
                    COALESCE(SUM(total_tokens), 0) AS tokens,
                    COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                FROM agent_runtime_metrics
                WHERE tenant_id=%s AND created_at >= %s
                GROUP BY 1
                ORDER BY 1
                """,
                (tenant_id, since),
            ).fetchall()
        return runtime_summary_from_aggregates(
            by_status={row["status"]: int(row["amount"]) for row in rows},
            by_model={row["model"]: int(row["amount"]) for row in models},
            totals=totals,
            daily_rows=daily_rows,
        )


def create_metrics_store(settings: Settings) -> MetricsStore:
    if settings.session_event_backend == "postgres":
        return PostgresMetricsStore(settings.postgres_dsn)
    return SQLiteMetricsStore(settings.runtime_metrics_path)
