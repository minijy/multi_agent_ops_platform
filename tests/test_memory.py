from pathlib import Path

import pytest

from ops_agent.config import Settings
from ops_agent.runtime.memory import (
    MemoryCreate,
    MemoryService,
    SQLiteMemoryStore,
    explicit_forget_requested,
    explicit_remember_requested,
    register_memory_tools,
)
from ops_agent.runtime.tools import ToolExecutionContext, ToolRegistry


def _service(tmp_path: Path, **overrides) -> MemoryService:
    settings = Settings(
        _env_file=None,
        memory_db_path=tmp_path / "memory.sqlite3",
        **overrides,
    )
    return MemoryService(SQLiteMemoryStore(settings.memory_db_path), settings)


def _context(**overrides) -> ToolExecutionContext:
    values = {
        "session_id": "session-a",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "role": "admin",
        "agent_id": "function-calling-runtime",
    }
    values.update(overrides)
    return ToolExecutionContext(**values)


def test_explicit_memory_intent_detection():
    assert explicit_remember_requested("请记住我喜欢用 CAD")
    assert not explicit_remember_requested("我喜欢用 CAD")
    assert explicit_forget_requested("请忘记这条记忆")


def test_remember_tool_requires_explicit_consent(tmp_path: Path):
    service = _service(tmp_path)
    registry = ToolRegistry()
    register_memory_tools(registry, service)
    definition = registry.get("remember_fact")
    arguments = definition.arguments_model.model_validate(
        {"content": "用户偏好 CAD 作为报表币种", "key": "report-currency"}
    )
    with pytest.raises(PermissionError):
        definition.handler(arguments, _context())
    result = definition.handler(
        arguments, _context(explicit_memory_consent=True)
    )
    assert result["remembered"] is True
    assert result["memory"]["status"] == "active"


def test_scope_isolation_search_and_delegation_snapshot(tmp_path: Path):
    service = _service(tmp_path)
    service.create(
        MemoryCreate(content="我的报表币种是 CAD", key="currency"),
        tenant_id="tenant-a", user_id="user-a", source="explicit",
    )
    service.create(
        MemoryCreate(content="公司财年从七月开始", key="fiscal-year", scope="tenant", kind="organization"),
        tenant_id="tenant-a", user_id="admin", source="admin",
    )
    service.create(
        MemoryCreate(content="利润分析使用实现利润", key="profit-basis", scope="agent", kind="agent", agent_id="profit-analyst"),
        tenant_id="tenant-a", user_id="admin", source="admin",
    )
    visible = service.build_snapshot(
        "报表利润", tenant_id="tenant-a", user_id="user-a", agent_id="profit-analyst"
    )
    assert {item["key"] for item in visible} == {"currency", "fiscal-year", "profit-basis"}
    other = service.build_snapshot(
        "报表利润", tenant_id="tenant-a", user_id="user-b", agent_id="erp-analyst"
    )
    assert {item["key"] for item in other} == {"fiscal-year"}


def test_candidate_conflict_confirmation_correction_and_profile(tmp_path: Path):
    service = _service(tmp_path)
    original = service.create(
        MemoryCreate(content="我的默认币种是 CAD", key="currency", scope="profile", kind="profile"),
        tenant_id="tenant-a", user_id="user-a", source="explicit",
    )
    candidate = service.create(
        MemoryCreate(content="我的默认币种是 USD", key="currency", scope="profile", kind="profile"),
        tenant_id="tenant-a", user_id="user-a", source="auto_candidate", status="candidate",
    )
    assert candidate.status == "conflicted"
    confirmed = service.confirm("tenant-a", candidate.id, replace_conflicts=True)
    assert confirmed.status == "active"
    assert service.store.get("tenant-a", original.id).status == "superseded"
    corrected = service.correct("tenant-a", confirmed.id, "我的默认币种是 EUR", "admin-a")
    assert corrected.correction_of == confirmed.id
    profile = service.profile("tenant-a", "user-a")
    assert profile["attributes"]["currency"] == "我的默认币种是 EUR"
    assert corrected.quality_score >= confirmed.quality_score


def test_auto_candidates_expiry_and_compliance_erasure(tmp_path: Path):
    service = _service(tmp_path, memory_default_expiry_days=30)
    candidates = service.extract_candidates(
        "我喜欢按月查看利润报表。我的时区是 Asia/Shanghai。",
        tenant_id="tenant-a", user_id="user-a", source_session_id="session-a",
    )
    assert candidates
    assert all(item.status in {"candidate", "conflicted"} for item in candidates)
    assert all(item.expires_at for item in candidates)
    count = service.compliance_delete_user("tenant-a", "user-a")
    assert count == len(candidates)
    stored = service.list("tenant-a", user_id="user-a", include_deleted=True)
    assert all(item.status == "deleted" and item.content == "[deleted]" for item in stored)

