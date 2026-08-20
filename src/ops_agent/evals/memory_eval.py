from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import get_settings
from ..runtime.memory import MemoryCreate, MemoryService, create_memory_service, memory_prompt


class MemoryEvalSeed(BaseModel):
    tenant_id: str
    user_id: str
    content: str
    key: str
    scope: str = "user"
    kind: str = "fact"
    agent_id: str | None = None
    importance: float = Field(default=0.7, ge=0, le=1)
    confidence: float = Field(default=0.95, ge=0, le=1)
    status: str = "active"
    expires_at: str | None = None
    delete_before_eval: bool = False


class MemoryEvalCase(BaseModel):
    name: str
    tenant_id: str
    user_id: str
    agent_id: str = "function-calling-runtime"
    query: str
    expected_keys: list[str] = Field(default_factory=list)
    forbidden_keys: list[str] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=50)


class MemoryEvalDataset(BaseModel):
    name: str = "memory-eval"
    memories: list[MemoryEvalSeed] = Field(default_factory=list)
    cases: list[MemoryEvalCase]


def seed_memory_dataset(
    service: MemoryService, seeds: list[MemoryEvalSeed]
) -> list[tuple[str, str]]:
    created: list[tuple[str, str]] = []
    try:
        for seed in seeds:
            item = service.create(
                MemoryCreate(
                    content=seed.content,
                    key=seed.key,
                    scope=seed.scope,
                    kind=seed.kind,
                    agent_id=seed.agent_id,
                    importance=seed.importance,
                    confidence=seed.confidence,
                ),
                tenant_id=seed.tenant_id,
                user_id=seed.user_id,
                source="enterprise_eval",
                source_session_id=f"eval-{seed.key}",
                status=seed.status,
            )
            created.append((seed.tenant_id, item.id))
            if seed.expires_at:
                item = service.store.put(
                    item.model_copy(update={"expires_at": seed.expires_at})
                )
            if seed.delete_before_eval:
                service.forget(seed.tenant_id, item.id, reason="eval_precondition")
    except Exception:
        for tenant_id, memory_id in created:
            service.forget(tenant_id, memory_id, reason="eval_seed_rollback")
        raise
    return created


def evaluate_memory(
    service: MemoryService, cases: list[MemoryEvalCase]
) -> dict[str, Any]:
    hits = 0
    expected = 0
    leakage = 0
    latencies: list[float] = []
    snapshot_chars: list[int] = []
    details: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter()
        results = service.search(
            case.query,
            tenant_id=case.tenant_id,
            user_id=case.user_id,
            agent_id=case.agent_id,
            limit=case.top_k,
        )
        latencies.append((time.perf_counter() - started) * 1000)
        snapshot_chars.append(len(memory_prompt(results)))
        keys = [str(item.get("key")) for item in results]
        case_hits = len(set(keys) & set(case.expected_keys))
        case_leaks = len(set(keys) & set(case.forbidden_keys))
        hits += case_hits
        expected += len(case.expected_keys)
        leakage += case_leaks
        details.append({
            "name": case.name,
            "returned_keys": keys,
            "expected_hits": case_hits,
            "leakage_count": case_leaks,
        })
    ordered = sorted(latencies)
    ordered_chars = sorted(snapshot_chars)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95))) if ordered else 0
    return {
        "case_count": len(cases),
        "recall_at_k": round(hits / expected, 4) if expected else 1.0,
        "cross_scope_leakage_count": leakage,
        "latency_ms_average": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        "latency_ms_p95": round(ordered[p95_index], 3) if ordered else 0.0,
        "snapshot_chars_average": round(sum(snapshot_chars) / len(snapshot_chars), 1)
        if snapshot_chars else 0.0,
        "snapshot_chars_p95": ordered_chars[p95_index] if ordered_chars else 0,
        "passed": leakage == 0 and (not expected or hits == expected),
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate ArkFlow long-term memory retrieval")
    parser.add_argument("dataset", type=Path, help="JSON array of MemoryEvalCase objects")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--keep-seed", action="store_true",
        help="Keep seeded evaluation memories after the run",
    )
    args = parser.parse_args()
    raw = json.loads(args.dataset.read_text())
    dataset = (
        MemoryEvalDataset(cases=[MemoryEvalCase.model_validate(item) for item in raw])
        if isinstance(raw, list)
        else MemoryEvalDataset.model_validate(raw)
    )
    service = create_memory_service(get_settings())
    seeded = seed_memory_dataset(service, dataset.memories)
    try:
        result = evaluate_memory(service, dataset.cases)
        result["dataset"] = dataset.name
        result["seeded_memory_count"] = len(seeded)
        result["evaluated_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        if not args.keep_seed:
            for tenant_id, memory_id in seeded:
                service.forget(tenant_id, memory_id, reason="eval_cleanup")
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
