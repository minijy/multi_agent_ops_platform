from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from ..runtime.agent_loop import AgentRuntime
from ..runtime.domain import RuntimeAgentRequest
from ..runtime.session_events import SessionEvent


class EvalCase(BaseModel):
    id: str
    question: str
    expect_status: str = "completed"
    expect_substrings: list[str] = Field(default_factory=list)
    expect_tools: list[str] = Field(default_factory=list)
    forbid_tools: list[str] = Field(default_factory=list)
    forbid_substrings: list[str] = Field(default_factory=list)


class EvalResult(BaseModel):
    id: str
    passed: bool
    failures: list[str] = Field(default_factory=list)
    status: str = ""
    answer: str = ""
    tools: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ReplayView:
    answer: str
    tools: list[str]
    leaked_protocol: bool


def project_replay(events: list[SessionEvent]) -> ReplayView:
    tools: list[str] = []
    answer = ""
    leaked = False
    for event in events:
        payload = event.payload or {}
        if event.event_type == "tool.requested":
            name = str(payload.get("tool_name") or "")
            if name:
                tools.append(name)
        elif event.event_type == "model.response":
            content = str(payload.get("content") or "")
            if '"finish_reason"' in content or '"tool_calls"' in content:
                leaked = True
            if content.strip():
                answer = content
        elif event.event_type == "turn.completed":
            answer = str(payload.get("answer") or answer)
    return ReplayView(answer=answer, tools=tools, leaked_protocol=leaked)


def load_eval_cases(path: Path) -> list[EvalCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [EvalCase.model_validate(item) for item in payload]


def run_eval_case(runtime: AgentRuntime, case: EvalCase, *, tenant_id: str) -> EvalResult:
    response = runtime.run(
        RuntimeAgentRequest(question=case.question),
        tenant_id=tenant_id,
        user_id="eval-runner",
        role="admin",
    )
    events = runtime.event_store.list_events(
        session_id=response.session_id, tenant_id=tenant_id
    )
    replay = project_replay(events)
    tools = [item.tool_name for item in response.tool_results] or replay.tools
    failures: list[str] = []
    if response.status != case.expect_status:
        failures.append(f"status {response.status} != {case.expect_status}")
    for snippet in case.expect_substrings:
        if snippet not in response.answer and snippet not in replay.answer:
            failures.append(f"missing substring: {snippet}")
    for snippet in case.forbid_substrings:
        if snippet in response.answer:
            failures.append(f"forbidden substring: {snippet}")
    for name in case.expect_tools:
        if name not in tools:
            failures.append(f"missing tool: {name}")
    for name in case.forbid_tools:
        if name in tools:
            failures.append(f"forbidden tool: {name}")
    if replay.leaked_protocol:
        failures.append("model leaked tool-call protocol JSON")
    return EvalResult(
        id=case.id,
        passed=not failures,
        failures=failures,
        status=response.status,
        answer=response.answer,
        tools=tools,
    )


def default_eval_path() -> Path:
    packaged = Path(__file__).resolve().parent / "golden.json"
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[3] / "evals" / "golden.json"


def _offline_runtime(event_path: Path) -> AgentRuntime:
    from ..config import Settings
    from ..runtime.model_router import create_model_router
    from ..runtime.session_events import SQLiteSessionEventStore
    from ..runtime.tools import ToolDefinition, ToolExecutor, ToolRegistry
    from ..workflows.amazon_finance.domain import AmazonFinanceQueryPlan

    settings = Settings(_env_file=None, model_provider="mock")
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="amazon_finance_query",
            description="query amazon released settlement metrics",
            arguments_model=AmazonFinanceQueryPlan,
            handler=lambda plan, _context: {
                "summary": "mock finance result",
                "plan": plan.model_dump(mode="json"),
                "rows": [],
            },
        )
    )
    return AgentRuntime(
        router=create_model_router(settings),
        registry=registry,
        executor=ToolExecutor(registry),
        event_store=SQLiteSessionEventStore(event_path),
    )


def main() -> int:
    cases = load_eval_cases(default_eval_path())
    runtime = _offline_runtime(Path("data/eval-events.sqlite3"))
    failed = 0
    for case in cases:
        result = run_eval_case(runtime, case, tenant_id="eval")
        mark = "PASS" if result.passed else "FAIL"
        print(f"{mark} {result.id}: {result.failures or 'ok'}")
        if not result.passed:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
