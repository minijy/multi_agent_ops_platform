import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel
from PIL import Image
import httpx
import pytest

from ops_agent.config import Settings
from ops_agent.runtime.agent_loop import AgentRuntime
from ops_agent.runtime.attachments import AttachmentError, LocalAttachmentStore
from ops_agent.runtime.domain import (
    AttachmentUploadRequest,
    ModelTurn,
    RuntimeAgentRequest,
    ToolCall,
)
from ops_agent.runtime.mcp_client import MCPClientManager
from ops_agent.runtime.model_errors import ModelProviderError
from ops_agent.runtime.model_router import ModelRouter, create_model_router
from ops_agent.runtime.session_events import SQLiteSessionEventStore
from ops_agent.runtime.skills import SkillRegistry, register_skill_tool
from ops_agent.runtime.tools import (
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
)


class EchoArguments(BaseModel):
    text: str


class FakeFunctionCallingAdapter:
    provider = "fake"
    model_name = "fake-tools"

    def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelTurn:
        if any(message.get("role") == "tool" for message in messages):
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                content="echo 工具调用完成",
            )
        return ModelTurn(
            provider=self.provider,
            model=self.model_name,
            tool_calls=[
                ToolCall(call_id="call-1", name="echo", arguments={"text": "hello"})
            ],
        )


def test_tool_registry_validates_and_executes():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo input",
            arguments_model=EchoArguments,
            handler=lambda args, _context: {"text": args.text},
        )
    )
    executor = ToolExecutor(registry)
    result = executor.execute(
        ToolCall(call_id="call-1", name="echo", arguments={"text": "hello"}),
        ToolExecutionContext(session_id="s", tenant_id="t", user_id="u"),
    )

    assert result.ok is True
    assert result.output == {"text": "hello"}
    assert registry.schemas()[0]["function"]["name"] == "echo"


def test_runtime_function_call_and_session_events(tmp_path: Path):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo input",
            arguments_model=EchoArguments,
            handler=lambda args, _context: {"text": args.text},
        )
    )
    event_store = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    adapter = FakeFunctionCallingAdapter()
    runtime = AgentRuntime(
        router=ModelRouter({"fake": adapter}, default_model_id="fake"),
        registry=registry,
        executor=ToolExecutor(registry),
        event_store=event_store,
    )

    response = runtime.run(
        RuntimeAgentRequest(question="call echo"),
        tenant_id="tenant-a",
        user_id="user-a",
    )
    events = event_store.list_events(
        session_id=response.session_id, tenant_id="tenant-a"
    )

    assert response.answer == "echo 工具调用完成"
    assert response.tool_results[0].output == {"text": "hello"}
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert {event.event_type for event in events} >= {
        "session.created",
        "user.message",
        "model.request",
        "model.response",
        "tool.requested",
        "tool.completed",
        "turn.completed",
    }
    completed = next(
        event for event in events if event.event_type == "turn.completed"
    )
    assert completed.payload["answer"] == "echo 工具调用完成"


def test_runtime_empty_model_content_still_records_final_answer(tmp_path: Path):
    class EmptyAdapter:
        provider = "fake"
        model_name = "fake-empty"
        input_modalities = frozenset({"text"})

        def invoke(self, _messages, _tools):
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                content="",
                usage={"total_tokens": 1},
            )

    event_store = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": EmptyAdapter()}, default_model_id="fake"),
        registry=ToolRegistry(),
        executor=ToolExecutor(ToolRegistry()),
        event_store=event_store,
    )
    response = runtime.run(
        RuntimeAgentRequest(question="空回复也应有结论"),
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert response.answer == "任务已完成。"
    completed = [
        event
        for event in event_store.list_events(
            session_id=response.session_id, tenant_id="tenant-a"
        )
        if event.event_type == "turn.completed"
    ]
    assert completed[-1].payload["answer"] == "任务已完成。"


def test_zhipu_adapter_uses_native_function_calling(monkeypatch):
    tool_call = SimpleNamespace(
        id="zhipu-call-1",
        function=SimpleNamespace(
            name="echo",
            arguments='{"text":"from glm"}',
        ),
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[tool_call])
            )
        ],
        usage=SimpleNamespace(
            model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5}
        ),
    )
    create = lambda **_kwargs: response
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    import zai

    client_options = []
    monkeypatch.setattr(
        zai,
        "ZhipuAiClient",
        lambda **kwargs: client_options.append(kwargs) or fake_client,
    )
    settings = Settings(
        _env_file=None,
        model_provider="zhipu",
        zai_api_key="test-key",
        zhipu_model_name="glm-test",
    )
    router = create_model_router(settings)
    turn = router.invoke(
        [{"role": "user", "content": "echo"}],
        [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "echo",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert turn.provider == "zhipu"
    assert turn.model == "glm-test"
    assert turn.tool_calls[0].arguments == {"text": "from glm"}
    assert turn.usage["prompt_tokens"] == 10
    assert client_options[0]["max_retries"] == 0
    assert client_options[0]["timeout"] == 45


def test_sanitize_assistant_content_strips_leaked_completion_json():
    from ops_agent.runtime.model_router import sanitize_assistant_content

    leaked = (
        "我需要使用工具来查询GLM-4.7-FlashX是否免费。\n"
        '{"index":0,"message":{"role":"assistant","content":"x",'
        '"tool_calls":[{"id":"c1","type":"function","function":'
        '{"name":"sandbox_full_access","arguments":'
        '"{\\"command\\":[\\"curl https://example.test\\"]}"}}]},'
        '"finish_reason":"tool_calls"}'
    )
    content, calls = sanitize_assistant_content(leaked)
    assert content == "我需要使用工具来查询GLM-4.7-FlashX是否免费。"
    assert "finish_reason" not in content
    assert calls[0].name == "sandbox_full_access"
    assert calls[0].arguments["command"] == ["curl https://example.test"]


def test_zhipu_adapter_hides_leaked_tool_json_from_content(monkeypatch):
    leaked = (
        "准备查询。"
        '{"index":0,"finish_reason":"tool_calls","tool_calls":[]}'
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=leaked, tool_calls=[])
            )
        ],
        usage=None,
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: response)
        )
    )
    import zai

    monkeypatch.setattr(zai, "ZhipuAiClient", lambda **_kwargs: fake_client)
    router = create_model_router(
        Settings(
            _env_file=None,
            model_provider="zhipu",
            zai_api_key="test-key",
        )
    )
    turn = router.invoke([{"role": "user", "content": "hi"}], [])
    assert turn.content == "准备查询。"
    assert "finish_reason" not in turn.content


def _zhipu_limit_error(code: str, message: str, retry_after: str | None = None):
    from zai.core._errors import APIReachLimitError

    headers = {"retry-after": retry_after} if retry_after else {}
    response = httpx.Response(
        429,
        headers=headers,
        json={"error": {"code": code, "message": message}},
        request=httpx.Request("POST", "https://example.test/chat"),
    )
    return APIReachLimitError(message, response=response)


def test_zhipu_overload_uses_bounded_backoff(monkeypatch):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=[])
            )
        ],
        usage=None,
    )
    calls = 0

    def create(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _zhipu_limit_error("1305", "busy")
        return response

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    import zai

    monkeypatch.setattr(zai, "ZhipuAiClient", lambda **_kwargs: fake_client)
    sleeps = []
    monkeypatch.setattr(
        "ops_agent.runtime.model_errors.random.uniform",
        lambda _start, end: end,
    )
    monkeypatch.setattr(
        "ops_agent.runtime.model_errors.time.sleep", sleeps.append
    )
    router = create_model_router(
        Settings(
            _env_file=None,
            model_provider="zhipu",
            zai_api_key="test-key",
            model_max_retries=1,
            model_backoff_base_seconds=0.5,
        )
    )
    turn = router.invoke([{"role": "user", "content": "hi"}], [])
    assert turn.content == "ok"
    assert calls == 2
    assert sleeps == [0.5]


def test_zhipu_rate_limit_fails_fast_with_friendly_error(monkeypatch):
    calls = 0

    def create(**_kwargs):
        nonlocal calls
        calls += 1
        raise _zhipu_limit_error("1302", "rate limit", "17")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    import zai

    monkeypatch.setattr(zai, "ZhipuAiClient", lambda **_kwargs: fake_client)
    router = create_model_router(
        Settings(
            _env_file=None,
            model_provider="zhipu",
            zai_api_key="test-key",
            model_max_retries=3,
        )
    )
    with pytest.raises(ModelProviderError) as captured:
        router.invoke([{"role": "user", "content": "hi"}], [])
    assert captured.value.code == "1302"
    assert captured.value.retry_after_seconds == 17
    assert "请求过于频繁" in captured.value.user_message
    assert calls == 1


def test_model_provider_error_accepts_traceback_assignment():
    error = ModelProviderError(
        provider="zhipu",
        code="1113",
        user_message="模型账户当前没有可用余额或资源包。",
        status_code=429,
    )
    error.__traceback__ = None
    assert error.code == "1113"


def test_runtime_propagates_balance_error_through_graph(tmp_path: Path):
    class FailingAdapter:
        provider = "fake"
        model_name = "fake-fail"
        input_modalities = frozenset({"text"})

        def invoke(self, _messages, _tools):
            raise ModelProviderError(
                provider="zhipu",
                code="1113",
                user_message="模型账户当前没有可用余额或资源包。",
                status_code=429,
            )

    event_store = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter({"fake": FailingAdapter()}, default_model_id="fake"),
        registry=ToolRegistry(),
        executor=ToolExecutor(ToolRegistry()),
        event_store=event_store,
    )
    with pytest.raises(ModelProviderError) as captured:
        runtime.run(
            RuntimeAgentRequest(question="触发余额错误"),
            tenant_id="tenant-a",
            user_id="user-a",
        )
    assert captured.value.code == "1113"
    assert "余额" in captured.value.user_message


def test_tool_visibility_is_shared_by_schema_and_dispatch():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="admin_only",
            description="restricted",
            arguments_model=EchoArguments,
            handler=lambda args, _context: args.text,
            allowed_roles=frozenset({"admin"}),
        )
    )
    viewer = ToolExecutionContext(
        session_id="s", tenant_id="t", user_id="u", role="viewer"
    )
    assert registry.schemas(viewer) == []
    result = ToolExecutor(registry).execute(
        ToolCall(
            call_id="restricted", name="admin_only", arguments={"text": "x"}
        ),
        viewer,
    )
    assert result.ok is False
    assert "not visible" in (result.error or "")


def test_skill_catalog_and_lazy_load(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "incident-triage"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: incident-triage\n"
        "description: Triage an incident safely.\n---\n\n# Secret body\n",
        encoding="utf-8",
    )
    skills = SkillRegistry.from_paths(str(tmp_path / "skills"))
    assert skills.list()[0].name == "incident-triage"
    assert "Secret body" not in skills.catalog_prompt()

    tools = ToolRegistry()
    register_skill_tool(tools, skills)
    result = ToolExecutor(tools).execute(
        ToolCall(
            call_id="skill-1",
            name="load_skill",
            arguments={"name": "incident-triage"},
        ),
        ToolExecutionContext(session_id="s", tenant_id="t", user_id="u"),
    )
    assert result.ok is True
    assert "Secret body" in result.output["content"]


def _png_base64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 3), color="red").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_attachment_store_validates_and_isolates_tenants(tmp_path: Path):
    store = LocalAttachmentStore(tmp_path / "attachments")
    reference = store.save(
        AttachmentUploadRequest(
            name="../chart.png",
            media_type="image/png",
            data_base64=_png_base64(),
        ),
        tenant_id="tenant-a",
    )
    assert reference.width == 2
    assert reference.height == 3
    assert reference.name == ".._chart.png"
    assert store.data_url(
        reference.attachment_id, tenant_id="tenant-a"
    ).startswith("data:image/png;base64,")
    try:
        store.get(reference.attachment_id, tenant_id="tenant-b")
    except AttachmentError as exc:
        assert str(exc) == "ATTACHMENT_NOT_FOUND"
    else:
        raise AssertionError("cross-tenant attachment access must fail")


def test_model_router_selects_image_capable_adapter():
    text_adapter = FakeFunctionCallingAdapter()
    vision_adapter = FakeFunctionCallingAdapter()
    vision_adapter.provider = "vision"
    vision_adapter.model_name = "vision-model"
    vision_adapter.input_modalities = frozenset({"text", "image"})
    router = ModelRouter(
        {"text": text_adapter, "text__vision": vision_adapter},
        default_model_id="text",
        vision_adapter_keys={"text": "text__vision"},
    )
    route = router.route(model_id="text", required_modalities={"text", "image"})
    assert route.adapter_key == "text__vision"
    assert route.model == "vision-model"


def test_runtime_sends_persisted_image_to_vision_route(tmp_path: Path):
    class VisionAdapter:
        provider = "vision"
        model_name = "vision-model"
        input_modalities = frozenset({"text", "image"})

        def invoke(self, messages, tools):
            user_content = messages[-1]["content"]
            assert user_content[1]["type"] == "image_url"
            assert user_content[1]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                content="图片已识别",
            )

    attachments = LocalAttachmentStore(tmp_path / "attachments")
    reference = attachments.save(
        AttachmentUploadRequest(
            name="chart.png",
            media_type="image/png",
            data_base64=_png_base64(),
        ),
        tenant_id="tenant-a",
    )
    events = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    runtime = AgentRuntime(
        router=ModelRouter(
            {"vision": VisionAdapter()}, default_model_id="vision"
        ),
        registry=ToolRegistry(),
        executor=ToolExecutor(ToolRegistry()),
        event_store=events,
        attachment_store=attachments,
    )
    response = runtime.run(
        RuntimeAgentRequest(
            question="分析这张图片",
            attachment_ids=[reference.attachment_id],
        ),
        tenant_id="tenant-a",
        user_id="user-a",
    )
    stored = events.list_events(
        session_id=response.session_id, tenant_id="tenant-a"
    )
    user_event = next(item for item in stored if item.event_type == "user.message")
    assert response.model == "vision-model"
    assert user_event.payload["attachment_ids"] == [reference.attachment_id]


def test_mcp_stdio_discovery_and_execution(tmp_path: Path):
    server = tmp_path / "server.py"
    server.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "app = FastMCP('test')\n"
        "@app.tool()\n"
        "def echo(text: str) -> dict:\n"
        "    return {'text': text}\n"
        "app.run()\n",
        encoding="utf-8",
    )
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "local",
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(server)],
                        "fail_on_startup_error": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    manager = MCPClientManager(config, registry)
    manager.start()
    try:
        assert registry.schemas()[0]["function"]["name"] == "mcp__local__echo"
        result = ToolExecutor(registry).execute(
            ToolCall(
                call_id="mcp-1",
                name="mcp__local__echo",
                arguments={"text": "hello"},
            ),
            ToolExecutionContext(session_id="s", tenant_id="t", user_id="u"),
        )
        assert result.ok is True
        assert "hello" in json.dumps(result.output)
    finally:
        manager.stop()


def test_restore_messages_keeps_prior_turns_and_compacts_old_tools(tmp_path: Path):
    class RecordingAdapter:
        provider = "fake"
        model_name = "fake-context"
        input_modalities = frozenset({"text"})

        def __init__(self) -> None:
            self.seen: list[list[dict[str, Any]]] = []

        def invoke(self, messages, _tools):
            self.seen.append(messages)
            users = [item["content"] for item in messages if item.get("role") == "user"]
            if messages[-1].get("role") == "tool":
                return ModelTurn(
                    provider=self.provider,
                    model=self.model_name,
                    content="上一轮表格用订单类型",
                )
            if users[-1] == "把底下的订单类型改成财务类型":
                assert "帮我分析费用" in users
                assert any(
                    item.get("role") == "assistant"
                    and "订单类型" in str(item.get("content") or "")
                    for item in messages
                )
                return ModelTurn(
                    provider=self.provider,
                    model=self.model_name,
                    content="| 财务类型 | 金额 |\n| --- | --- |\n| Commission | 1 |",
                )
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                content="",
                tool_calls=[
                    ToolCall(
                        call_id="call-hist",
                        name="echo",
                        arguments={"text": "first"},
                    )
                ],
            )

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo input",
            arguments_model=EchoArguments,
            handler=lambda args, _context: {
                "summary": "ok",
                "columns": ["n"],
                "rows": [{"n": i} for i in range(40)],
            },
        )
    )
    adapter = RecordingAdapter()
    runtime = AgentRuntime(
        router=ModelRouter({"fake": adapter}, default_model_id="fake"),
        registry=registry,
        executor=ToolExecutor(registry),
        event_store=SQLiteSessionEventStore(tmp_path / "events.sqlite3"),
    )
    first = runtime.run(
        RuntimeAgentRequest(question="帮我分析费用"),
        tenant_id="tenant-a",
        user_id="user-a",
    )
    second = runtime.run(
        RuntimeAgentRequest(
            question="把底下的订单类型改成财务类型",
            session_id=first.session_id,
        ),
        tenant_id="tenant-a",
        user_id="user-a",
    )
    follow_up = adapter.seen[-1]
    assert follow_up[0]["role"] == "system"
    assert any(item.get("role") == "user" and item.get("content") == "帮我分析费用" for item in follow_up)
    old_tool = next(item for item in follow_up if item.get("role") == "tool")
    payload = json.loads(old_tool["content"])
    assert payload["rows_truncated"] is True
    assert len(payload["rows"]) == 12
    assert "财务类型" in second.answer


def test_compact_tool_content_truncates_rows():
    compact = json.loads(
        AgentRuntime._compact_tool_content(
            json.dumps({"rows": [{"n": i} for i in range(30)], "summary": "x"})
        )
    )
    assert compact["rows_truncated"] is True
    assert compact["row_count"] == 30
    assert len(compact["rows"]) == 12


def test_prepare_model_messages_nulls_empty_tool_call_content(tmp_path: Path):
    runtime = AgentRuntime(
        router=ModelRouter({"fake": FakeFunctionCallingAdapter()}, default_model_id="fake"),
        registry=ToolRegistry(),
        executor=ToolExecutor(ToolRegistry()),
        event_store=SQLiteSessionEventStore(tmp_path / "events.sqlite3"),
    )
    prepared = runtime._prepare_model_messages(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"rows": [{"n": i} for i in range(20)]})},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "q2"},
        ]
    )
    assert prepared[2]["content"] is None
    assert json.loads(prepared[3]["content"])["rows_truncated"] is True
    assert prepared[-1]["content"] == "q2"


def test_prepare_model_messages_sliding_window_drops_old_turns(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        context_window_enabled=True,
        context_keep_recent_user_turns=1,
        context_max_messages=8,
        context_max_chars=80_000,
    )
    runtime = AgentRuntime(
        router=ModelRouter({"fake": FakeFunctionCallingAdapter()}, default_model_id="fake"),
        registry=ToolRegistry(),
        executor=ToolExecutor(ToolRegistry()),
        event_store=SQLiteSessionEventStore(tmp_path / "events.sqlite3"),
        settings=settings,
    )
    prepared = runtime._prepare_model_messages(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function"}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "old-tool"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new question"},
        ]
    )
    assert [item["role"] for item in prepared] == ["system", "user"]
    assert prepared[0]["content"] == "sys"
    assert prepared[1]["content"] == "new question"

    settings.context_window_enabled = False
    kept = runtime._prepare_model_messages(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new question"},
        ]
    )
    assert [item["content"] for item in kept if item["role"] == "user"] == [
        "old question",
        "new question",
    ]


def test_continue_session_resumes_open_turn_without_new_user_message(tmp_path: Path):
    class ResumeAdapter:
        provider = "fake"
        model_name = "fake-resume"
        input_modalities = frozenset({"text"})

        def invoke(self, messages, _tools):
            assert messages[-1]["role"] == "tool"
            users = [item["content"] for item in messages if item.get("role") == "user"]
            assert users == ["分析费用"]
            return ModelTurn(
                provider=self.provider,
                model=self.model_name,
                content="续上的结论",
            )

    from ops_agent.runtime.agent_loop import turn_is_open
    from ops_agent.runtime.domain import ToolResult

    event_store = SQLiteSessionEventStore(tmp_path / "events.sqlite3")
    session_id = "11111111-1111-1111-1111-111111111111"
    event_store.append(
        session_id=session_id, tenant_id="tenant-a", user_id="user-a",
        event_type="session.created", payload={"seller_id": None, "role": "admin"},
    )
    event_store.append(
        session_id=session_id, tenant_id="tenant-a", user_id="user-a",
        event_type="user.message", payload={"content": "分析费用", "attachment_ids": []},
    )
    event_store.append(
        session_id=session_id, tenant_id="tenant-a", user_id="user-a",
        event_type="model.response",
        payload={
            "provider": "fake",
            "model": "fake-resume",
            "content": "",
            "tool_calls": [{"call_id": "call-open", "name": "echo", "arguments": {"text": "x"}}],
        },
    )
    event_store.append(
        session_id=session_id, tenant_id="tenant-a", user_id="user-a",
        event_type="tool.completed",
        payload=ToolResult(
            call_id="call-open", tool_name="echo", ok=True, output={"summary": "ok"}
        ).model_dump(mode="json"),
    )
    assert turn_is_open(event_store.list_events(session_id=session_id, tenant_id="tenant-a"))

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo input",
            arguments_model=EchoArguments,
            handler=lambda args, _context: {"text": args.text},
        )
    )
    runtime = AgentRuntime(
        router=ModelRouter({"fake": ResumeAdapter()}, default_model_id="fake"),
        registry=registry,
        executor=ToolExecutor(registry),
        event_store=event_store,
    )
    result = runtime.continue_session(
        session_id=session_id, tenant_id="tenant-a", user_id="user-a"
    )
    events = event_store.list_events(session_id=session_id, tenant_id="tenant-a")
    assert result.answer == "续上的结论"
    assert [event.event_type for event in events if event.event_type == "user.message"] == ["user.message"]
    assert not turn_is_open(events)
