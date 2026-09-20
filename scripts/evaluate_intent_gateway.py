from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate intent-router predictions through its API")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default="qwen3-1.7b-intent-router")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--validator-dir", type=Path)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("concurrency must be positive")
    return args


def load_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_validator(path: Path | None) -> Callable[[dict[str, Any], dict[str, Any]], Any] | None:
    if path is None:
        return None
    sys.path.insert(0, str(path.resolve()))
    from schema_rules import validate_router_label

    return validate_router_label


def parse_prediction(raw: str) -> dict[str, Any]:
    value = json.loads(raw.strip())
    if not isinstance(value, dict):
        raise ValueError("prediction must be a JSON object")
    return value


async def evaluate(args: argparse.Namespace) -> list[dict[str, Any]]:
    records = load_records(args.dataset)
    validator = load_validator(args.validator_dir)
    results: list[dict[str, Any] | None] = [None] * len(records)
    semaphore = asyncio.Semaphore(args.concurrency)
    headers = {"authorization": f"Bearer {args.api_key}"} if args.api_key else {}

    async with httpx.AsyncClient(timeout=args.timeout) as client:

        async def predict(index: int, record: dict[str, Any]) -> None:
            item_id = str((record.get("metadata") or {}).get("id") or "")
            raw = ""
            prediction: dict[str, Any] = {}
            error = ""
            async with semaphore:
                try:
                    response = await client.post(
                        f"{args.url.rstrip('/')}/v1/chat/completions",
                        headers=headers,
                        json={
                            "model": args.model,
                            "messages": record["messages"][:2],
                            "temperature": 0,
                            "max_tokens": 128,
                            "repetition_penalty": 1.0,
                            "chat_template_kwargs": {"enable_thinking": False},
                            "stream": False,
                        },
                    )
                    response.raise_for_status()
                    raw = str(response.json()["choices"][0]["message"]["content"]).strip()
                    prediction = parse_prediction(raw)
                    if validator is not None:
                        model_input = json.loads(record["messages"][-2]["content"])
                        prediction = validator(model_input, prediction, enforce_grounding=True)
                except Exception as exc:
                    error = str(exc)
            results[index] = {
                "id": item_id,
                "prediction": prediction,
                "parse_error": error,
                "raw": raw,
            }

        await asyncio.gather(*(predict(index, record) for index, record in enumerate(records)))

    return [result for result in results if result is not None]


def main() -> int:
    args = parse_args()
    results = asyncio.run(evaluate(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(args.output), "predictions": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
