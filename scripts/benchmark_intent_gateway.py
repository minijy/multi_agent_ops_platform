from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any

import httpx


DEFAULT_PAYLOAD = {
    "model": "intent-router",
    "messages": [
        {
            "role": "system",
            "content": (
                "你是意图路由器，只输出JSON。decision只能是direct_answer、tool_plan、"
                "clarify、abstain。/no_think"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "query": "查询2026年7月Amazon结算费用Top 5",
                    "history": [],
                    "visible_tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "delegate_subagent",
                                "description": "委派单一领域任务",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "agent_id": {"type": "string"},
                                        "objective": {"type": "string"},
                                    },
                                    "required": ["agent_id", "objective"],
                                },
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ],
    "temperature": 0,
    "max_tokens": 128,
    "stream": False,
}

ALLOWED_DECISIONS = {"direct_answer", "tool_plan", "clarify", "abstain"}


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def valid_intent_output(response: httpx.Response) -> bool:
    try:
        content = response.json()["choices"][0]["message"]["content"]
        start = content.find("{")
        end = content.rfind("}")
        output = json.loads(content[start : end + 1])
        return output.get("decision") in ALLOWED_DECISIONS
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return False


async def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    payload = DEFAULT_PAYLOAD
    if args.payload:
        payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    payload = {**payload, "model": args.model}
    semaphore = asyncio.Semaphore(args.concurrency)
    headers = {"authorization": f"Bearer {args.api_key}"}
    latencies: list[float] = []
    status_counts: dict[int, int] = {}
    valid_outputs = 0

    async with httpx.AsyncClient(timeout=args.timeout) as client:

        async def one(index: int) -> None:
            nonlocal valid_outputs
            async with semaphore:
                started = time.perf_counter()
                response = await client.post(
                    f"{args.url.rstrip('/')}/v1/chat/completions",
                    headers={
                        **headers,
                        "x-request-id": f"benchmark-{index}",
                        "x-tenant-id": "benchmark",
                        "x-user-id": f"user-{index % max(1, args.users)}",
                    },
                    json=payload,
                )
                latencies.append(time.perf_counter() - started)
                status_counts[response.status_code] = status_counts.get(response.status_code, 0) + 1
                if response.status_code == 200 and (
                    args.skip_output_validation or valid_intent_output(response)
                ):
                    valid_outputs += 1

        started = time.perf_counter()
        await asyncio.gather(*(one(index) for index in range(args.requests)))
        elapsed = time.perf_counter() - started

    success = status_counts.get(200, 0)
    return {
        "requests": args.requests,
        "users": args.users,
        "concurrency": args.concurrency,
        "elapsed_seconds": round(elapsed, 3),
        "throughput_requests_per_second": round(args.requests / elapsed, 3),
        "status_counts": status_counts,
        "success_rate": round(success / args.requests, 4),
        "valid_output_rate": round(valid_outputs / args.requests, 4),
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50) * 1000, 2),
            "p95": round(percentile(latencies, 0.95) * 1000, 2),
            "p99": round(percentile(latencies, 0.99) * 1000, 2),
            "max": round(max(latencies, default=0) * 1000, 2),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load-test the intent inference gateway")
    parser.add_argument("--url", default="http://127.0.0.1:8200")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", default="intent-router")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--payload")
    parser.add_argument("--skip-output-validation", action="store_true")
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.users < 1:
        parser.error("requests, concurrency and users must be positive")
    return args


def main() -> None:
    print(json.dumps(asyncio.run(benchmark(parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
