import json

from pydantic import BaseModel

from ops_agent.runtime.agent_loop import AgentRuntime
from ops_agent.runtime.domain import ModelTurn, RuntimeAgentRequest, ToolCall
from ops_agent.runtime.model_router import ModelRouter
from ops_agent.runtime.result_store import (
    SQLiteResultStore,
    StoredResult,
    materialize_tool_output,
    result_page,
)
from ops_agent.runtime.session_events import SQLiteSessionEventStore
from ops_agent.runtime.tools import ToolDefinition, ToolExecutor, ToolRegistry


class QueryArguments(BaseModel):
    limit: int = 40


class RecordingAdapter:
    provider = "fake"
    model_name = "fake"

    def __init__(self) -> None:
        self.messages = []

    def invoke(self, messages, tools):
        self.messages.append(messages)
        if not any(item.get("role") == "tool" for item in messages):
            return ModelTurn(
                provider="fake",
                model="fake",
                tool_calls=[
                    ToolCall(
                        call_id="query-1",
                        name="query_rows",
                        arguments={"limit": 40},
                    )
                ],
            )
        return ModelTurn(provider="fake", model="fake", content="统计完成")


def test_materialized_result_keeps_full_rows_and_returns_compact_projection(tmp_path):
    store = SQLiteResultStore(tmp_path / "events.sqlite3")
    output = {
        "columns": ["name", "amount"],
        "rows": [{"name": f"item-{index}", "amount": index} for index in range(30)],
        "summary": "30 rows",
        "total_rows": 300,
    }
    compact = materialize_tool_output(
        store,
        output,
        tenant_id="tenant-a",
        user_id="alice",
        session_id="session-a",
        tool_name="query_rows",
        preview_rows=5,
    )

    assert compact["returned_rows"] == 30
    assert len(compact["rows"]) == 5
    assert compact["rows_truncated"] is True
    assert compact["data_quality"]["source_rows"] == 300
    assert compact["statistics"]["numeric_columns"]["amount"]["sum"] == "435"

    record = store.get(compact["result_ref"], "tenant-a")
    assert record is not None
    page = result_page(record, offset=10, limit=7)
    assert [row["amount"] for row in page["rows"]] == list(range(10, 17))
    assert page["has_more"] is True


def test_runtime_never_sends_full_current_tool_rows_to_model(tmp_path):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="query_rows",
            description="query rows",
            arguments_model=QueryArguments,
            handler=lambda args, _context: {
                "columns": ["n"],
                "rows": [{"n": index} for index in range(args.limit)],
                "summary": "deterministic result",
                "total_rows": args.limit,
            },
        )
    )
    adapter = RecordingAdapter()
    store = SQLiteResultStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": adapter}, default_model_id="fake"),
        registry=registry,
        executor=ToolExecutor(registry),
        event_store=SQLiteSessionEventStore(tmp_path / "events.sqlite3"),
        result_store=store,
    )
    response = runtime.run(
        RuntimeAgentRequest(question="统计数据"),
        tenant_id="tenant-a",
        user_id="alice",
    )

    tool_message = next(
        item for item in adapter.messages[-1] if item.get("role") == "tool"
    )
    model_payload = json.loads(tool_message["content"])
    assert len(model_payload["rows"]) == 12
    assert model_payload["returned_rows"] == 40
    assert model_payload["result_ref"].startswith("result-")
    assert len(json.dumps(model_payload)) < 4000
    assert response.answer == "统计完成"
    assert response.tool_results[0].output["rows_truncated"] is True


def test_result_store_deletes_results_with_session(tmp_path):
    store = SQLiteResultStore(tmp_path / "events.sqlite3")
    record = StoredResult(
        result_ref="result-delete",
        tenant_id="tenant-a",
        user_id="alice",
        session_id="session-delete",
        tool_name="query_rows",
        payload={"rows": [{"n": 1}]},
        created_at="2026-08-18T00:00:00+00:00",
    )
    store.put(record)
    assert store.delete_session("session-delete", "tenant-a") == 1
    assert store.get("result-delete", "tenant-a") is None
