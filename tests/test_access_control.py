import pytest

from ops_agent.access_control import AccessControlStore, ToolAssignmentConflict
from ops_agent.agent_roles import SYSTEM_DEFAULT_TOOL_NAMES


def test_rbac_compatibility_and_effective_tool_union(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)

    compatibility = store.effective_access("tenant-a", "unknown")
    assert compatibility.configured is False
    assert compatibility.allowed_tools is None

    store.put_user("tenant-a", "alice", "Alice")
    store.put_group("tenant-a", "finance", "Finance")
    store.put_rule("tenant-a", "read-profit", "Read profit", ["profit_report_query"])
    store.bind_user_group("tenant-a", "alice", "finance")
    store.bind_group_rule("tenant-a", "finance", "read-profit")

    allowed = store.effective_access("tenant-a", "alice")
    assert allowed.configured is True
    assert allowed.allowed_tools == SYSTEM_DEFAULT_TOOL_NAMES | {"profit_report_query"}
    assert allowed.group_ids == ("finance",)
    assert allowed.rule_ids == ("profit_report_query",)

    denied = store.effective_access("tenant-a", "bob")
    assert denied.user_exists is False
    assert denied.allowed_tools == frozenset()


def test_disabled_user_has_no_tools(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)
    store.put_user("tenant-a", "alice", "Alice", enabled=False)
    result = store.effective_access("tenant-a", "alice")
    assert result.user_enabled is False
    assert result.allowed_tools == frozenset()


def test_group_directly_configures_multiple_tools_and_tools_are_reusable(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)
    store.put_user("tenant-a", "alice", "Alice")
    store.put_group("tenant-a", "finance", "Finance")
    store.put_group("tenant-a", "operations", "Operations")
    store.bind_user_group("tenant-a", "alice", "finance")

    finance = store.set_group_tools(
        "tenant-a", "finance", ["profit_report_query", "amazon_finance_query"]
    )
    operations = store.set_group_tools(
        "tenant-a", "operations", ["profit_report_query"]
    )

    assert finance["tool_names"] == ["amazon_finance_query", "profit_report_query"]
    assert operations["tool_names"] == ["profit_report_query"]
    assert store.effective_access("tenant-a", "alice").allowed_tools == (
        SYSTEM_DEFAULT_TOOL_NAMES | {"amazon_finance_query", "profit_report_query"}
    )

    replaced = store.set_group_tools("tenant-a", "finance", ["kingdee_cloud_query"])
    assert replaced["tool_names"] == ["kingdee_cloud_query"]
    assert store.effective_access("tenant-a", "alice").allowed_tools == (
        SYSTEM_DEFAULT_TOOL_NAMES | {"kingdee_cloud_query"}
    )

    reopened = AccessControlStore(postgres_dsn)
    assert reopened.get_group("tenant-a", "finance")["tool_names"] == [
        "kingdee_cloud_query"
    ]


def test_enabled_user_gets_runtime_tools_without_permission_group(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)
    store.put_user("tenant-a", "alice", "Alice")

    result = store.effective_access("tenant-a", "alice", role="operator")

    assert result.allowed_tools == SYSTEM_DEFAULT_TOOL_NAMES
    assert result.group_ids == ()


def test_system_tools_are_removed_from_permission_rules(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)
    store.put_group("tenant-a", "finance", "Finance")
    rule = store.put_rule(
        "tenant-a", "rule-a", "Rule A",
        ["profit_report_query", "delegate_subagent", "load_skill"],
        group_id="finance",
    )

    assert rule["tool_names"] == ["profit_report_query"]


def test_permission_rule_belongs_to_only_one_group(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)
    store.put_group("tenant-a", "finance", "Finance")
    store.put_group("tenant-a", "operations", "Operations")
    store.put_rule(
        "tenant-a", "read-profit", "Read profit", ["profit_report_query"],
        group_id="finance",
    )

    assert store.get_group("tenant-a", "finance")["rule_ids"] == ["read-profit"]
    store.bind_group_rule("tenant-a", "operations", "read-profit")

    assert store.get_group("tenant-a", "finance")["rule_ids"] == []
    assert store.get_group("tenant-a", "operations")["rule_ids"] == ["read-profit"]
    assert store.get_rule("tenant-a", "read-profit")["group_id"] == "operations"


def test_tool_is_unique_within_group_but_reusable_across_groups(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)
    store.put_group("tenant-a", "finance", "Finance")
    store.put_group("tenant-a", "operations", "Operations")
    store.put_rule(
        "tenant-a", "rule-a", "Rule A", ["profit_report_query"],
        group_id="finance",
    )

    with pytest.raises(ToolAssignmentConflict):
        store.put_rule(
            "tenant-a", "rule-b", "Rule B", ["profit_report_query"],
            group_id="finance",
        )

    assert store.get_rule("tenant-a", "rule-b") is None

    reused = store.put_rule(
        "tenant-a", "rule-c", "Rule C", ["profit_report_query"],
        group_id="operations",
    )
    assert reused["tool_names"] == ["profit_report_query"]


def test_moving_rule_rejects_duplicate_tool_in_target_group(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)
    store.put_group("tenant-a", "finance", "Finance")
    store.put_group("tenant-a", "operations", "Operations")
    store.put_rule(
        "tenant-a", "rule-a", "Rule A", ["profit_report_query"],
        group_id="finance",
    )
    store.put_rule(
        "tenant-a", "rule-b", "Rule B", ["profit_report_query"],
        group_id="operations",
    )

    with pytest.raises(ToolAssignmentConflict):
        store.bind_group_rule("tenant-a", "finance", "rule-b")

    assert store.get_rule("tenant-a", "rule-b")["group_id"] == "operations"


def test_admin_bypasses_user_group_and_rule_restrictions(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)
    store.put_user("tenant-a", "disabled-admin", "Disabled Admin", enabled=False)
    store.put_user("tenant-a", "alice", "Alice")

    disabled = store.effective_access(
        "tenant-a", "disabled-admin", role="admin"
    )
    unregistered = store.effective_access(
        "tenant-a", "local-admin", role="admin"
    )

    assert disabled.user_enabled is True
    assert disabled.allowed_tools is None
    assert unregistered.user_enabled is True
    assert unregistered.allowed_tools is None


def test_permission_denial_detail_explains_missing_binding(tmp_path, postgres_dsn):
    store = AccessControlStore(postgres_dsn)
    store.put_user("tenant-a", "alice", "Alice")

    detail = store.effective_access(
        "tenant-a", "alice", role="operator"
    ).denial_detail("profit_report_query")

    assert detail["code"] == "permission_group_missing"
    assert "权限组" in detail["message"]
    assert "管理员" in detail["hint"]
