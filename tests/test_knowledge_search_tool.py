from ops_agent.agent_registry import create_agent_registry
from ops_agent.config import Settings
from ops_agent.knowledge_gateway import KnowledgeGateway
from ops_agent.runtime.agent_tool_policy import resolve_agent_tool_allowlist
from ops_agent.runtime.knowledge_search_tool import register_search_knowledge_tool
from ops_agent.runtime.tools import ToolDefinition, ToolExecutionContext, ToolRegistry


def _context(**overrides) -> ToolExecutionContext:
    values = {
        "session_id": "session-a",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "role": "operator",
        "agent_id": "function-calling-runtime",
    }
    values.update(overrides)
    return ToolExecutionContext(**values)


class _FakeGateway(KnowledgeGateway):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:8000", "token")
        self.calls: list[tuple[str, str, str]] = []

    def list_spaces(self, tenant_id: str):
        self.calls.append(("list", "", tenant_id))
        if tenant_id != "tenant-a":
            return []
        return [{"id": "kb-1", "name": "技术文档", "tenant_id": tenant_id}]

    def search_space(self, tenant_id, space_id, *, query, top_k=5, category_ids=None):
        self.calls.append(("search", space_id, tenant_id))
        assert tenant_id == "tenant-a"
        return {
            "items": [
                {
                    "document_id": "doc-auth",
                    "chunk_id": "chunk-1",
                    "title": "认证故障手册",
                    "page": 4,
                    "category_id": "ops",
                    "score": 0.91,
                    "text": "AUTH-1003 需要清理失效会话并重新登录。",
                    "knowledge_space_id": space_id,
                }
            ]
        }


def test_search_knowledge_returns_tenant_filtered_citations():
    gateway = _FakeGateway()
    registry = ToolRegistry()
    register_search_knowledge_tool(registry, gateway)
    definition = registry.get("search_knowledge")
    arguments = definition.arguments_model.model_validate({"query": "AUTH-1003"})
    result = definition.handler(arguments, _context())
    assert result["ok"] is True
    assert result["items"][0]["title"] == "认证故障手册"
    assert result["items"][0]["page"] == 4
    assert result["items"][0]["document_id"] == "doc-auth"
    assert "认证故障手册" in result["summary"]
    assert gateway.calls[0] == ("list", "", "tenant-a")
    assert gateway.calls[1] == ("search", "kb-1", "tenant-a")


def test_search_knowledge_rejects_other_tenant_space():
    gateway = _FakeGateway()
    registry = ToolRegistry()
    register_search_knowledge_tool(registry, gateway)
    definition = registry.get("search_knowledge")
    arguments = definition.arguments_model.model_validate(
        {"query": "AUTH-1003", "space_id": "kb-1"}
    )
    result = definition.handler(arguments, _context(tenant_id="tenant-b"))
    assert result["items"] == []
    assert "不属于当前租户" in result["summary"]
    assert all(call[0] != "search" for call in gateway.calls)


def test_search_knowledge_unconfigured():
    registry = ToolRegistry()
    register_search_knowledge_tool(registry, KnowledgeGateway())
    definition = registry.get("search_knowledge")
    result = definition.handler(
        definition.arguments_model.model_validate({"query": "制度"}),
        _context(),
    )
    assert result["configured"] is False
    assert result["items"] == []


def test_coordinator_allowlist_includes_search_knowledge(tmp_path):
    from pydantic import BaseModel

    class _Args(BaseModel):
        value: str = "x"

    settings = Settings(_env_file=None, agent_definitions_path=tmp_path / "agents.json")
    agents = create_agent_registry(settings.agent_definitions_path)
    registry = ToolRegistry()
    register_search_knowledge_tool(registry, KnowledgeGateway("http://wenshu", "token"))
    for name in (
        "delegate_subagent",
        "delegate_specialists",
        "load_skill",
        "search_memory",
        "remember_fact",
        "forget_memory",
    ):
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                arguments_model=_Args,
                handler=lambda *_args, **_kwargs: {},
                builtin=True,
            )
        )
    allowed = resolve_agent_tool_allowlist(
        agents.runtime_config(), agents, settings, registry
    )
    assert "search_knowledge" in allowed
    analyst = resolve_agent_tool_allowlist(
        agents.analyst_config(), agents, settings, registry
    )
    assert "search_knowledge" not in analyst
