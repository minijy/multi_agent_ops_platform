from __future__ import annotations

from typing import Any

from ops_agent.agent_registry import AgentDefinition
from ops_agent.workflows.kingdee_cloud.domain import KingdeeIntegrationConfig
from ops_agent.workflows.lingxing_profit.domain import LingXingIntegrationConfig

SECRET_MASK = "********"


def lingxing_integration_configured(integration: dict[str, Any] | None) -> bool:
    if not integration:
        return False
    app_id = str(integration.get("app_id") or "").strip()
    app_secret = str(integration.get("app_secret") or "").strip()
    return bool(app_id and app_secret)


def kingdee_integration_configured(integration: dict[str, Any] | None) -> bool:
    if not integration:
        return False
    required = (
        "server_url",
        "acct_id",
        "app_id",
        "app_secret",
        "username",
    )
    return all(str(integration.get(key) or "").strip() for key in required)


def mask_agent_integration(agent: AgentDefinition) -> dict[str, Any]:
    payload = agent.model_dump(mode="json")
    integration = payload.get("integration")
    if not isinstance(integration, dict):
        return payload
    masked = dict(integration)
    if agent.id in {"lingxing-profit-report", "kingdee-cloud"}:
        secret = str(integration.get("app_secret") or "")
        masked["app_secret"] = SECRET_MASK if secret else ""
        masked["app_secret_configured"] = bool(secret)
    payload["integration"] = masked
    return payload


def merge_integration_update(
    agent_id: str,
    current: dict[str, Any] | None,
    patch: dict[str, Any] | None,
) -> dict[str, Any]:
    if agent_id == "kingdee-cloud":
        base = KingdeeIntegrationConfig.model_validate(current or {}).model_dump()
        if not patch:
            return base
        merged = {**base, **{k: v for k, v in patch.items() if v is not None}}
        incoming_secret = str(merged.get("app_secret") or "")
        if incoming_secret in {"", SECRET_MASK}:
            merged["app_secret"] = str((current or {}).get("app_secret") or "")
        return KingdeeIntegrationConfig.model_validate(merged).model_dump()

    base = LingXingIntegrationConfig.model_validate(current or {}).model_dump()
    if not patch:
        return base
    merged = {**base, **{k: v for k, v in patch.items() if v is not None}}
    incoming_secret = str(merged.get("app_secret") or "")
    if incoming_secret in {"", SECRET_MASK}:
        merged["app_secret"] = str((current or {}).get("app_secret") or "")
    return LingXingIntegrationConfig.model_validate(merged).model_dump()
