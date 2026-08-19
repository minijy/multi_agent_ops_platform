#!/usr/bin/env python3
"""Load-test PostgreSQL query APIs without calling the model.

Sends prebuilt `plan` payloads to:
  POST /v1/amazon-finance/query
  POST /v1/profit-report/query

Usage:
  .venv/bin/python scripts/load_pg_query.py
  .venv/bin/python scripts/load_pg_query.py --concurrency 20 --requests 200
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE = os.environ.get("OPS_AGENT_BASE_URL", "http://127.0.0.1:8100")


def discover_seller_id() -> str | None:
    try:
        from ops_agent.config import Settings
        from psycopg import connect
        from psycopg.rows import dict_row
    except ImportError:
        return None
    dsn = Settings().analytics_dsn
    if not dsn:
        return None
    with connect(dsn, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT seller_id, count(*) AS n
                FROM amazon_finance_transactions
                GROUP BY seller_id
                ORDER BY n DESC
                LIMIT 5
                """
            )
            rows = list(cursor.fetchall())
    if not rows:
        return None
    print(
        "amazon sellers: "
        + ", ".join(f"{row['seller_id']} ({row['n']} rows)" for row in rows)
    )
    return str(rows[0]["seller_id"])


@dataclass
class Sample:
    name: str
    status: int
    latency_ms: float
    rows: int
    error: str = ""


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def request_json(
    base: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], float]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Tenant-ID": os.environ.get("OPS_AGENT_TENANT", "tenant-a"),
            "X-User-ID": "load-tester",
            "X-User-Role": "admin",
            **({"X-API-Key": key} if (key := os.environ.get("APP_API_KEY", "").strip()) else {}),
            **(headers or {}),
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            latency = (time.perf_counter() - started) * 1000
            data = json.loads(raw.decode("utf-8")) if raw else {}
            return response.status, data if isinstance(data, dict) else {}, latency
    except urllib.error.HTTPError as exc:
        latency = (time.perf_counter() - started) * 1000
        raw = exc.read()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            data = {"detail": raw.decode("utf-8", errors="replace")}
        if not isinstance(data, dict):
            data = {"detail": str(data)}
        return exc.code, data, latency


def error_text(payload: dict[str, Any]) -> str:
    detail = payload.get("detail", payload)
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


def amazon_cases(seller_id: str | None) -> list[dict[str, Any]]:
    seller = {"seller_id": seller_id} if seller_id else {}
    return [
        {
            "name": "amazon.overview",
            "path": "/v1/amazon-finance/query",
            "payload": {
                "question": "load-test amazon overview",
                **seller,
                "plan": {"metric": "overview"},
            },
        },
        {
            "name": "amazon.daily",
            "path": "/v1/amazon-finance/query",
            "payload": {
                "question": "load-test amazon daily",
                **seller,
                "plan": {"metric": "daily", "limit": 30},
            },
        },
        {
            "name": "amazon.fee",
            "path": "/v1/amazon-finance/query",
            "payload": {
                "question": "load-test amazon fee",
                **seller,
                "plan": {"metric": "fee", "limit": 20},
            },
        },
        {
            "name": "amazon.sku",
            "path": "/v1/amazon-finance/query",
            "payload": {
                "question": "load-test amazon sku",
                **seller,
                "plan": {"metric": "sku", "limit": 20},
            },
        },
    ]


def profit_cases() -> list[dict[str, Any]]:
    return [
        {
            "name": "profit.overview",
            "path": "/v1/profit-report/query",
            "payload": {
                "question": "load-test profit overview",
                "plan": {"metric": "overview"},
            },
        },
        {
            "name": "profit.daily",
            "path": "/v1/profit-report/query",
            "payload": {
                "question": "load-test profit daily",
                "plan": {"metric": "daily", "limit": 30},
            },
        },
        {
            "name": "profit.store",
            "path": "/v1/profit-report/query",
            "payload": {
                "question": "load-test profit store",
                "plan": {"metric": "store", "limit": 20},
            },
        },
        {
            "name": "profit.msku",
            "path": "/v1/profit-report/query",
            "payload": {
                "question": "load-test profit msku",
                "plan": {"metric": "msku", "limit": 20},
            },
        },
    ]


def run_one(base: str, case: dict[str, Any], timeout: float) -> Sample:
    status, payload, latency = request_json(
        base,
        case["path"],
        method="POST",
        payload=case["payload"],
        timeout=timeout,
    )
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    return Sample(
        name=case["name"],
        status=status,
        latency_ms=latency,
        rows=len(rows),
        error="" if status == 200 else error_text(payload),
    )


def print_group(title: str, samples: list[Sample]) -> None:
    ok = [item for item in samples if item.status == 200]
    failed = [item for item in samples if item.status != 200]
    latencies = [item.latency_ms for item in ok]
    print(f"\n== {title} ==")
    print(f"requests={len(samples)} ok={len(ok)} failed={len(failed)}")
    if latencies:
        print(
            "latency_ms  "
            f"min={min(latencies):.1f}  "
            f"p50={percentile(latencies, 0.50):.1f}  "
            f"p95={percentile(latencies, 0.95):.1f}  "
            f"p99={percentile(latencies, 0.99):.1f}  "
            f"avg={statistics.fmean(latencies):.1f}  "
            f"max={max(latencies):.1f}"
        )
        print(f"rows_avg={statistics.fmean(item.rows for item in ok):.1f}")
    if failed:
        by_status: dict[int, int] = {}
        for item in failed:
            by_status[item.status] = by_status.get(item.status, 0) + 1
        print(f"errors={by_status}")
        for item in failed[:5]:
            print(f"  {item.name} {item.status}: {item.error[:240]}")


def parse_targets(raw: str) -> set[str]:
    items = {item.strip() for item in raw.split(",") if item.strip()}
    allowed = {"amazon", "profit"}
    unknown = items - allowed
    if unknown:
        raise SystemExit(f"unknown --targets: {', '.join(sorted(unknown))}")
    return items or allowed


def main() -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL query API load test (no model calls)")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--targets", default="amazon,profit", help="amazon,profit")
    parser.add_argument("--seller-id", default="")
    args = parser.parse_args()
    targets = parse_targets(args.targets)

    health_status, health, _ = request_json(args.base_url, "/health", timeout=5)
    if health_status != 200:
        print(f"service unavailable: {args.base_url}/health -> {health_status} {health}")
        return 1
    print(
        f"health ok  env={health.get('environment')}  "
        f"amazon_finance={health.get('amazon_finance')}  "
        f"profit_report={health.get('profit_report')}"
    )

    seller_id = args.seller_id.strip() or None
    cases: list[dict[str, Any]] = []
    if "amazon" in targets:
        if not seller_id:
            seller_id = discover_seller_id()
        warmup = run_one(args.base_url, amazon_cases(seller_id)[0], args.timeout)
        if warmup.status != 200:
            print(f"amazon warmup failed: {warmup.status} {warmup.error}")
            return 1
        print(f"amazon warmup ok  seller_id={seller_id or '-'}  {warmup.latency_ms:.1f}ms")
        cases.extend(amazon_cases(seller_id))
    if "profit" in targets:
        warmup = run_one(args.base_url, profit_cases()[0], args.timeout)
        if warmup.status != 200:
            print(f"profit warmup failed: {warmup.status} {warmup.error}")
            return 1
        print(f"profit warmup ok  rows={warmup.rows}  {warmup.latency_ms:.1f}ms")
        cases.extend(profit_cases())

    workload = [cases[index % len(cases)] for index in range(args.requests)]
    print(
        f"\nrunning {args.requests} requests  concurrency={args.concurrency}  "
        f"cases={', '.join(case['name'] for case in cases)}"
    )
    samples: list[Sample] = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(run_one, args.base_url, case, args.timeout) for case in workload]
        for future in as_completed(futures):
            samples.append(future.result())
    elapsed = time.perf_counter() - started
    ok = [item for item in samples if item.status == 200]
    print(f"wall_seconds={elapsed:.2f}  throughput={len(samples) / elapsed:.1f} req/s")
    print_group("all", samples)
    names = sorted({item.name for item in samples})
    for name in names:
        print_group(name, [item for item in samples if item.name == name])
    return 0 if len(ok) == len(samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
