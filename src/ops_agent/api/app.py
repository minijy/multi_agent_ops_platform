from __future__ import annotations

import json
import logging
import mimetypes
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field, ValidationError
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..agent_integration import mask_agent_integration
from ..access_control import ToolAssignmentConflict
from ..accounts import AccountError, create_account_service
from ..connections import (
    ConnectionCreateRequest,
    ConnectionUpdateRequest,
    ConnectionUpsertRequest,
)
from ..agent_roles import (
    ANALYST_AGENT_ID,
    COORDINATOR_AGENT_ID,
    DATA_QUERY_TOOL_NAMES,
    SYSTEM_DEFAULT_TOOL_NAMES,
    SPECIALIST_ANALYST_IDS,
)
from ..runtime.agent_tool_policy import (
    amazon_finance_tool_active,
    kingdee_cloud_tool_active,
    lingxing_profit_tool_active,
    profit_report_tool_active,
)
from ..agent_registry import AgentRegistry, AgentUpdateRequest, snapshot_agents
from ..config import (
    Settings,
    analyst_runtime_snapshot,
    context_window_snapshot,
    get_settings,
    update_analyst_runtime,
    update_context_window,
)
from ..domain import ApprovalRequest
from ..infrastructure.platform_store import PlatformStore, create_platform_store
from ..model_gateway import create_model_gateway
from ..knowledge_gateway import KnowledgeGateway, register_knowledge_library_routes
from ..knowledge_spaces import KnowledgeSpaceCreate, KnowledgeSpaceUpdate
from ..model_registry import (
    ModelCreateRequest,
    ModelRegistry,
    ModelUpdateRequest,
    mask_model_definition,
)
from ..runtime.agent_loop import AgentRuntime, turn_is_open
from ..runtime.attachments import AttachmentError
from ..runtime.domain import (
    AttachmentReference,
    AttachmentUploadRequest,
    ContextWindowUpdate,
    ResumeAgentRequest,
    RuntimeAgentRequest,
    RuntimeAgentResponse,
    ToolCall,
)
from ..runtime.auth import principal_from_bearer
from ..runtime.model_errors import ModelProviderError
from ..runtime.model_router import create_model_router_from_registry
from ..runtime.memory import MemoryCreate
from ..runtime.result_store import result_page
from ..runtime.session_events import SessionEvent
from ..runtime.connectors import ToolConnectionBindingRequest
from ..runtime.stack import open_runtime_stack
from ..runtime.subagents import SubagentSubmitRequest
from ..runtime.tools import ToolExecutionContext, ToolExecutor
from ..source_privacy import sanitize_public_value
from ..workflows.amazon_finance.agent import AmazonFinanceAgent, SYSTEM_PROMPT as AMAZON_SYSTEM_PROMPT
from ..workflows.amazon_finance.domain import (
    AmazonFinanceQueryRequest,
    AmazonFinanceQueryResponse,
)
from ..workflows.amazon_finance.query_tool import AmazonFinanceQueryTool
from ..workflows.kingdee_cloud.agent import (
    KingdeeCloudAgent,
    SYSTEM_PROMPT as KINGDEE_SYSTEM_PROMPT,
)
from ..workflows.kingdee_cloud.domain import (
    KingdeeIntegrationConfig,
    KingdeeQueryRequest,
    KingdeeQueryResponse,
)
from ..workflows.kingdee_cloud.query_tool import KingdeeQueryTool
from ..workflows.lingxing_profit.agent import (
    LingXingProfitAgent,
    SYSTEM_PROMPT as LINGXING_SYSTEM_PROMPT,
)
from ..workflows.lingxing_profit.domain import (
    LingXingIntegrationConfig,
    LingXingProfitQueryRequest,
    LingXingProfitQueryResponse,
)
from ..workflows.lingxing_profit.query_tool import LingXingProfitQueryTool
from ..workflows.profit_report.agent import (
    ProfitReportAgent,
    SYSTEM_PROMPT as PROFIT_REPORT_SYSTEM_PROMPT,
)
from ..workflows.profit_report.domain import (
    ProfitReportQueryRequest,
    ProfitReportQueryResponse,
)
from ..workflows.profit_report.query_tool import ProfitReportQueryTool


LOGGER = logging.getLogger(__name__)
ROLES = {"viewer", "operator", "approver", "admin"}


class AnalystRuntimeUpdate(BaseModel):
    mode: Literal["general", "specialized_parallel"]


class AccessUserUpsert(BaseModel):
    id: str
    name: str
    enabled: bool = True
    role: Literal["viewer", "operator", "approver", "admin"] = "viewer"
    temporary_password: str | None = Field(default=None, min_length=10, max_length=256)
    generate_temporary_password: bool = False


class AccountRegisterRequest(BaseModel):
    tenant_id: str = Field(min_length=2, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    user_id: str = Field(min_length=2, max_length=128, pattern=r"^[A-Za-z0-9_.@-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=10, max_length=256)


class AccountLoginRequest(BaseModel):
    tenant_id: str = Field(min_length=2, max_length=128)
    user_id: str = Field(min_length=2, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=512)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class ResetPasswordRequest(BaseModel):
    temporary_password: str | None = Field(default=None, min_length=10, max_length=256)
    generate_temporary_password: bool = True


class PermissionGroupUpsert(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200, pattern=r".*\S.*")
    description: str = Field(default="", max_length=1000)


class PermissionGroupToolsUpdate(BaseModel):
    tool_names: list[str]


class PermissionRuleUpsert(BaseModel):
    id: str | None = None
    group_id: str
    name: str
    description: str = ""
    tool_names: list[str]


class AccessBindingRequest(BaseModel):
    target_id: str


class MemoryDecisionRequest(BaseModel):
    replace_conflicts: bool = True


class MemoryCorrectionRequest(BaseModel):
    content: str = Field(min_length=2, max_length=12000)


class MemoryDeleteRequest(BaseModel):
    reason: str = Field(default="admin_requested", max_length=500)


class MemoryAdminCreateRequest(MemoryCreate):
    owner_user_id: str | None = Field(default=None, max_length=128)


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    role: str


def _amazon_finance_active(
    settings: Settings, registry: AgentRegistry, connections=None, tenant_id=None
) -> bool:
    return amazon_finance_tool_active(registry, settings, connections, tenant_id)


def _lingxing_profit_active(registry: AgentRegistry, connections=None, tenant_id=None) -> bool:
    return lingxing_profit_tool_active(registry, connections, tenant_id)


def _profit_report_active(
    settings: Settings, registry: AgentRegistry, connections=None, tenant_id=None
) -> bool:
    return profit_report_tool_active(registry, settings, connections, tenant_id)


def _kingdee_cloud_active(registry: AgentRegistry, connections=None, tenant_id=None) -> bool:
    return kingdee_cloud_tool_active(registry, connections, tenant_id)


def _sync_amazon_finance_agent(
    agent: AmazonFinanceAgent, registry: AgentRegistry
) -> None:
    config = registry.amazon_finance_config()
    agent.set_system_prompt(config.effective_system_prompt(AMAZON_SYSTEM_PROMPT))


def _sync_lingxing_profit_agent(
    agent: LingXingProfitAgent, registry: AgentRegistry
) -> None:
    config = registry.lingxing_profit_config()
    agent.set_system_prompt(config.effective_system_prompt(LINGXING_SYSTEM_PROMPT))
    raw = config.integration if isinstance(config.integration, dict) else {}
    agent.set_integration(LingXingIntegrationConfig.model_validate(raw))


def _sync_profit_report_agent(
    agent: ProfitReportAgent, registry: AgentRegistry
) -> None:
    config = registry.profit_report_config()
    agent.set_system_prompt(config.effective_system_prompt(PROFIT_REPORT_SYSTEM_PROMPT))


def _sync_kingdee_cloud_agent(
    agent: KingdeeCloudAgent, registry: AgentRegistry
) -> None:
    config = registry.kingdee_cloud_config()
    agent.set_system_prompt(config.effective_system_prompt(KINGDEE_SYSTEM_PROMPT))
    raw = config.integration if isinstance(config.integration, dict) else {}
    agent.set_integration(KingdeeIntegrationConfig.model_validate(raw))


def _reload_model_router(application: FastAPI) -> None:
    runtime: AgentRuntime = application.state.agent_runtime
    registry: ModelRegistry = application.state.model_registry
    settings: Settings = application.state.settings
    registry.reload()
    runtime.reload_router(create_model_router_from_registry(registry, settings))


def create_app(settings: Settings | None = None) -> FastAPI:
    configured_settings = settings

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime_settings = configured_settings or get_settings()
        runtime_settings.validate_runtime()
        logging.basicConfig(level=runtime_settings.log_level)
        model = create_model_gateway(runtime_settings)
        application.state.settings = runtime_settings
        application.state.store = create_platform_store(runtime_settings)
        application.state.account_service = create_account_service(runtime_settings)
        application.state.amazon_finance_agent = AmazonFinanceAgent(
            model,
            AmazonFinanceQueryTool(
                "",
                statement_timeout_ms=runtime_settings.analytics_statement_timeout_ms,
            ),
        )
        application.state.lingxing_profit_agent = LingXingProfitAgent(
            model,
            LingXingProfitQueryTool(),
        )
        application.state.profit_report_agent = ProfitReportAgent(
            model,
            ProfitReportQueryTool(
                "",
                statement_timeout_ms=runtime_settings.analytics_statement_timeout_ms,
            ),
        )
        application.state.kingdee_cloud_agent = KingdeeCloudAgent(
            model,
            KingdeeQueryTool(),
        )
        with open_runtime_stack(runtime_settings) as stack:
            application.state.agent_registry = stack.agent_registry
            application.state.model_registry = stack.model_registry
            application.state.connection_registry = stack.connection_registry
            application.state.connector_runtime = stack.connector_runtime
            application.state.tool_bindings = stack.tool_bindings
            application.state.access_control = stack.access_control
            application.state.result_store = stack.result_store
            application.state.memory_service = stack.memory_service
            application.state.knowledge_spaces = stack.knowledge_spaces
            application.state.knowledge_gateway = KnowledgeGateway.from_settings(
                runtime_settings
            )
            application.state.runtime_tool_registry = stack.tool_registry
            application.state.runtime_tool_executor = stack.agent_runtime.executor
            _sync_amazon_finance_agent(
                application.state.amazon_finance_agent,
                stack.agent_registry,
            )
            _sync_lingxing_profit_agent(
                application.state.lingxing_profit_agent,
                stack.agent_registry,
            )
            _sync_profit_report_agent(
                application.state.profit_report_agent,
                stack.agent_registry,
            )
            _sync_kingdee_cloud_agent(
                application.state.kingdee_cloud_agent,
                stack.agent_registry,
            )
            application.state.session_events = stack.session_events
            application.state.metrics_store = stack.metrics_store
            application.state.attachment_store = stack.attachment_store
            application.state.skill_registry = stack.skill_registry
            application.state.mcp_manager = stack.mcp_manager
            application.state.agent_runtime = stack.agent_runtime
            application.state.runtime_governance = stack.governance_store
            application.state.subagent_manager = stack.subagent_manager
            application.state.sandbox_runner = stack.sandbox_runner
            yield

    application = FastAPI(
        title="Multi-Agent Ops Platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    def authorize(
        request: Request,
        api_key: str | None,
        tenant_id: str | None,
        user_id: str | None,
        role: str | None,
        allowed_roles: set[str] | None = None,
    ) -> Principal:
        settings = request.app.state.settings
        bearer = request.headers.get("authorization") or ""
        if bearer.lower().startswith("bearer "):
            token = bearer.split(" ", 1)[1].strip()
            try:
                claims = request.app.state.account_service.principal(token)
            except AccountError as account_error:
                if not settings.jwt_secret:
                    raise HTTPException(
                        status_code=account_error.status_code, detail=account_error.detail()
                    ) from account_error
                claims = principal_from_bearer(token, settings)
            resolved_role = claims["role"]
            if resolved_role not in ROLES:
                raise HTTPException(status_code=400, detail="invalid role")
            account = request.app.state.account_service.account(
                claims["tenant_id"], claims["user_id"]
            )
            if account and account["must_change_password"]:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "password_change_required",
                        "message": "首次登录必须先修改临时密码。",
                        "hint": "请在修改密码页面设置新密码后继续。",
                    },
                )
            if allowed_roles and resolved_role not in allowed_roles:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "role_not_allowed",
                        "message": "当前角色无权执行此操作。",
                        "hint": "请使用具备相应角色的账号，或联系管理员调整角色。",
                    },
                )
            return Principal(
                tenant_id=claims["tenant_id"],
                user_id=claims["user_id"],
                role=resolved_role,
            )
        if settings.jwt_required:
            raise HTTPException(status_code=401, detail="bearer token required")
        expected = settings.app_api_key
        if expected and api_key != expected:
            raise HTTPException(status_code=401, detail="invalid API key")
        resolved_role = role or "admin"
        if resolved_role not in ROLES:
            raise HTTPException(status_code=400, detail="invalid role")
        if allowed_roles and resolved_role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "role_not_allowed",
                    "message": "当前角色无权执行此操作。",
                    "hint": "请使用具备相应角色的账号，或联系管理员调整角色。",
                },
            )
        resolved_tenant = tenant_id or settings.default_tenant_id
        if request.app.state.account_service.configured(resolved_tenant):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "login_required",
                    "message": "该租户已启用账户登录，请先登录。",
                },
            )
        return Principal(
            tenant_id=resolved_tenant,
            user_id=user_id or "local-admin",
            role=resolved_role,
        )

    def principal_from_headers(
        request: Request,
        x_api_key: str | None,
        x_tenant_id: str | None,
        x_user_id: str | None,
        x_user_role: str | None,
        allowed_roles: set[str] | None = None,
    ) -> Principal:
        return authorize(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, allowed_roles
        )

    def owned_session_events(
        request: Request,
        principal: Principal,
        session_id: str,
    ) -> list[SessionEvent]:
        """Resolve a personal chat without disclosing another user's session."""
        events = request.app.state.session_events.list_events(
            session_id=session_id,
            tenant_id=principal.tenant_id,
        )
        owner = next(
            (
                event.user_id
                for event in events
                if event.event_type == "session.created"
            ),
            events[0].user_id if events else None,
        )
        if not events or owner != principal.user_id:
            raise HTTPException(status_code=404, detail="agent session not found")
        return events

    def agent_visible_for_access(agent: Any, allowed_tools: frozenset[str] | None) -> bool:
        if agent.id not in SPECIALIST_ANALYST_IDS or allowed_tools is None:
            return True
        required = set(agent.allowed_tools) & DATA_QUERY_TOOL_NAMES
        return bool(required & set(allowed_tools))

    def execute_direct_tool(
        request: Request,
        principal: Principal,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        decision = request.app.state.access_control.effective_access(
            principal.tenant_id, principal.user_id, principal.role
        )
        if decision.allowed_tools is not None and tool_name not in decision.allowed_tools:
            raise HTTPException(status_code=403, detail=decision.denial_detail(tool_name))
        try:
            connection_ids, resolved_scope = request.app.state.tool_bindings.execution_scope(
                principal.tenant_id,
                {tool_name},
                request.app.state.connection_registry,
            )
        except (KeyError, PermissionError, ValueError) as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "connection_scope_denied",
                    "message": f"工具 {tool_name} 没有可用的连接或数据范围权限。",
                    "hint": "请联系管理员检查工具绑定的 Connection 和资源范围。",
                    "reason": str(exc),
                    "tool_name": tool_name,
                },
            ) from exc
        resource_scope = {
            name: tuple(values) for name, values in resolved_scope.items()
        }
        call = ToolCall(
            call_id=f"direct-{uuid.uuid4().hex}",
            name=tool_name,
            arguments=arguments,
        )
        context = ToolExecutionContext(
            session_id=f"direct-{uuid.uuid4().hex}",
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            role=principal.role,
            allowed_tool_names=frozenset({tool_name}),
            connection_ids=tuple(connection_ids),
            resource_scope=resource_scope,
            connection_scope_enforced=True,
        )
        executor: ToolExecutor = request.app.state.runtime_tool_executor
        result = executor.execute(call, context)
        if not result.ok:
            reason = result.error or "tool execution failed"
            permission_markers = (
                "permission", "not visible", "not authorized", "access denied",
                "outside delegated scope", "no authorized resources",
            )
            if any(marker in reason.lower() for marker in permission_markers):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "tool_execution_denied",
                        "message": f"工具 {tool_name} 的执行被权限策略拒绝。",
                        "hint": "请联系管理员检查工具权限、连接和数据范围。",
                        "reason": reason,
                        "tool_name": tool_name,
                    },
                )
            raise HTTPException(status_code=400, detail=reason)
        if not isinstance(result.output, dict):
            raise HTTPException(status_code=500, detail="tool returned an invalid response")
        return result.output

    def account_failure(error: AccountError) -> HTTPException:
        return HTTPException(status_code=error.status_code, detail=error.detail())

    def bearer_account(request: Request) -> tuple[Principal, dict[str, Any]]:
        bearer = request.headers.get("authorization") or ""
        if not bearer.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail={"code": "login_required", "message": "请先登录。"},
            )
        try:
            claims = request.app.state.account_service.principal(
                bearer.split(" ", 1)[1].strip()
            )
        except AccountError as error:
            raise account_failure(error) from error
        account = request.app.state.account_service.account(
            claims["tenant_id"], claims["user_id"]
        )
        if not account:
            raise HTTPException(status_code=401, detail="account not found")
        return Principal(**claims), account

    def connection_payload(request: Request, connection) -> dict[str, Any]:
        registry = request.app.state.connection_registry
        return registry.masked_values(connection) | {
            "id": connection.id,
            "tenant_id": connection.tenant_id,
            "connector_type": connection.connector_type,
            "name": connection.name,
            "enabled": connection.enabled,
        }

    @application.post("/v1/auth/register", status_code=201)
    def register_account(payload: AccountRegisterRequest, request: Request) -> dict[str, Any]:
        if request.app.state.settings.app_env == "production":
            raise HTTPException(status_code=403, detail="self-service registration is disabled")
        service = request.app.state.account_service
        try:
            account = service.register(
                payload.tenant_id, payload.user_id, payload.display_name, payload.password
            )
        except AccountError as error:
            raise account_failure(error) from error
        request.app.state.access_control.put_user(
            payload.tenant_id, payload.user_id, payload.display_name, True
        )
        request.app.state.store.audit(
            tenant_id=payload.tenant_id, actor_id=payload.user_id,
            actor_role=account["role"], action="account.registered",
            resource_type="account", resource_id=payload.user_id,
            detail={"role": account["role"]},
        )
        return service.issue_tokens(account)

    @application.post("/v1/auth/login")
    def login_account(payload: AccountLoginRequest, request: Request) -> dict[str, Any]:
        service = request.app.state.account_service
        try:
            account = service.authenticate(payload.tenant_id, payload.user_id, payload.password)
        except AccountError as error:
            request.app.state.store.audit(
                tenant_id=payload.tenant_id, actor_id=payload.user_id,
                actor_role="unknown", action="account.login_failed",
                resource_type="account", resource_id=payload.user_id,
                detail={"code": error.code},
            )
            raise account_failure(error) from error
        request.app.state.store.audit(
            tenant_id=payload.tenant_id, actor_id=payload.user_id,
            actor_role=account["role"], action="account.login_succeeded",
            resource_type="account", resource_id=payload.user_id, detail={},
        )
        return service.issue_tokens(account)

    @application.post("/v1/auth/refresh")
    def refresh_account(payload: RefreshTokenRequest, request: Request) -> dict[str, Any]:
        try:
            return request.app.state.account_service.refresh(payload.refresh_token)
        except AccountError as error:
            raise account_failure(error) from error

    @application.post("/v1/auth/logout", status_code=204)
    def logout_account(payload: RefreshTokenRequest, request: Request) -> Response:
        service = request.app.state.account_service
        service.store.revoke_session(service._token_hash(payload.refresh_token))
        return Response(status_code=204)

    @application.get("/v1/auth/me")
    def current_account(request: Request) -> dict[str, Any]:
        _, account = bearer_account(request)
        return account

    @application.post("/v1/auth/change-password")
    def change_account_password(
        payload: ChangePasswordRequest, request: Request
    ) -> dict[str, Any]:
        principal, _ = bearer_account(request)
        try:
            account = request.app.state.account_service.change_password(
                principal.tenant_id, principal.user_id,
                payload.current_password, payload.new_password,
            )
        except AccountError as error:
            raise account_failure(error) from error
        request.app.state.store.audit(
            tenant_id=principal.tenant_id, actor_id=principal.user_id,
            actor_role=principal.role, action="account.password_changed",
            resource_type="account", resource_id=principal.user_id, detail={},
        )
        return request.app.state.account_service.issue_tokens(account)

    @application.get("/health")
    def health(request: Request) -> dict[str, Any]:
        settings = request.app.state.settings
        configured_models = [
            model
            for model in request.app.state.model_registry.list(enabled_only=True)
            if model.callable()
        ]
        default_model = next(
            (model for model in configured_models if model.is_default),
            configured_models[0] if configured_models else None,
        )
        return {
            "status": "ok", "environment": settings.app_env,
            "control_plane": settings.control_plane_backend,
            "session_events": settings.session_event_backend,
            "model_provider": (
                default_model.provider
                if default_model
                else "unconfigured"
            ),
            "knowledge_backend": (
                "configured"
                if request.app.state.knowledge_spaces.list(
                    settings.default_tenant_id
                )
                else "disabled"
            ),
            "amazon_finance": (
                "configured"
                if request.app.state.connection_registry.get_default(
                    settings.default_tenant_id, "analytics"
                )
                else "disabled"
            ),
            "lingxing_profit": (
                "configured"
                if _lingxing_profit_active(
                    request.app.state.agent_registry,
                    request.app.state.connection_registry,
                )
                else "disabled"
            ),
            "profit_report": (
                "configured"
                if _profit_report_active(
                    settings,
                    request.app.state.agent_registry,
                    request.app.state.connection_registry,
                )
                else "disabled"
            ),
            "kingdee_cloud": (
                "configured"
                if _kingdee_cloud_active(
                    request.app.state.agent_registry,
                    request.app.state.connection_registry,
                )
                else "disabled"
            ),
            "agent_runtime": "ready",
            "subagents": settings.subagent_queue_backend,
            "sandbox": (
                "seatbelt"
                if request.app.state.sandbox_runner.restricted_available
                else "full-access-only"
            ),
            "otel_exporter": settings.otel_exporter,
            "jwt": "required" if settings.jwt_required else "optional",
        }

    @application.get("/v1/dashboard/summary")
    def dashboard_summary(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        store: PlatformStore = request.app.state.store
        runtime = request.app.state.metrics_store.summarize(principal.tenant_id)
        pending = request.app.state.runtime_governance.list_pending_approvals(
            principal.tenant_id
        )
        return {
            **store.summary(principal.tenant_id),
            "waiting_approval": len(pending),
            "environment": request.app.state.settings.app_env,
            "runtime": runtime.model_dump(mode="json"),
        }

    @application.post(
        "/v1/agent/query",
        response_model=RuntimeAgentResponse,
    )
    def query_agent_runtime(
        payload: RuntimeAgentRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> RuntimeAgentResponse:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        decision = request.app.state.access_control.effective_access(
            principal.tenant_id, principal.user_id, principal.role
        )
        if decision.configured and not decision.user_enabled:
            raise HTTPException(status_code=403, detail=decision.denial_detail())
        if payload.session_id:
            owned_session_events(request, principal, payload.session_id)
        try:
            return request.app.state.agent_runtime.run(
                payload,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                role=principal.role,
                token_budget=request.app.state.settings.run_token_budget,
                allowed_tools=(
                    set(decision.allowed_tools)
                    if decision.allowed_tools is not None
                    else None
                ),
            )
        except ModelProviderError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.as_dict(),
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post("/v1/agent/query/stream")
    def stream_agent_query(
        payload: RuntimeAgentRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> StreamingResponse:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        decision = request.app.state.access_control.effective_access(
            principal.tenant_id, principal.user_id, principal.role
        )
        if decision.configured and not decision.user_enabled:
            raise HTTPException(status_code=403, detail=decision.denial_detail())
        if payload.session_id:
            owned_session_events(request, principal, payload.session_id)
        events: Queue[dict[str, Any] | None] = Queue()

        def emit(item: dict[str, Any]) -> None:
            events.put(item)

        def worker() -> None:
            try:
                result = request.app.state.agent_runtime.run(
                    payload,
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    role=principal.role,
                    token_budget=request.app.state.settings.run_token_budget,
                    interruption_is_resumable=True,
                    on_event=emit,
                    allowed_tools=(
                        set(decision.allowed_tools)
                        if decision.allowed_tools is not None
                        else None
                    ),
                )
                events.put({"type": "done", **result.model_dump(mode="json")})
            except ModelProviderError as exc:
                events.put(
                    {"type": "error", **exc.as_dict(), "status_code": exc.status_code}
                )
            except Exception as exc:
                events.put(
                    {
                        "type": "error",
                        "code": type(exc).__name__,
                        "message": str(exc),
                        "provider": "runtime",
                    }
                )
            finally:
                events.put(None)

        threading.Thread(
            target=worker, daemon=True, name="agent-query-stream"
        ).start()

        def sse() -> Iterator[str]:
            while True:
                item = events.get()
                if item is None:
                    break
                name = str(item.get("type") or "message")
                yield (
                    f"event: {name}\n"
                    f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
                )

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def _sse(events: Queue[dict[str, Any] | None]) -> StreamingResponse:
        def sse() -> Iterator[str]:
            while True:
                item = events.get()
                if item is None:
                    break
                name = str(item.get("type") or "message")
                yield (
                    f"event: {name}\n"
                    f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
                )

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @application.post("/v1/agent/query/resume")
    def resume_agent_query(
        payload: ResumeAgentRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> StreamingResponse:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        runtime: AgentRuntime = request.app.state.agent_runtime
        stored = owned_session_events(request, principal, payload.session_id)
        events: Queue[dict[str, Any] | None] = Queue()

        def emit(item: dict[str, Any]) -> None:
            events.put(item)

        attached, live = runtime.live_hub.subscribe(payload.session_id)
        if live and attached is not None:
            def forward() -> None:
                try:
                    while True:
                        item = attached.get()
                        if item is None:
                            break
                        events.put(item)
                finally:
                    runtime.live_hub.unsubscribe(payload.session_id, attached)
                    events.put(None)

            threading.Thread(
                target=forward, daemon=True, name="agent-query-resume-attach"
            ).start()
            return _sse(events)

        if not turn_is_open(stored):
            completed = next(
                (
                    event
                    for event in reversed(stored)
                    if event.event_type == "turn.completed"
                ),
                None,
            )
            model = next(
                (
                    event
                    for event in reversed(stored)
                    if event.event_type == "model.response"
                ),
                None,
            )
            emit({"type": "session", "session_id": payload.session_id})
            emit(
                {
                    "type": "done",
                    "session_id": payload.session_id,
                    "answer": (completed.payload or {}).get("answer", "") if completed else "",
                    "provider": (model.payload or {}).get("provider", "") if model else "",
                    "model": (model.payload or {}).get("model", "") if model else "",
                    "status": (completed.payload or {}).get("status", "completed") if completed else "completed",
                    "pending_approval_ids": [],
                    "tool_results": [],
                    "event_count": len(stored),
                }
            )
            events.put(None)
            return _sse(events)

        def worker() -> None:
            try:
                result = runtime.continue_session(
                    session_id=payload.session_id,
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    role=principal.role,
                    token_budget=request.app.state.settings.run_token_budget,
                    interruption_is_resumable=True,
                    on_event=emit,
                )
                events.put({"type": "done", **result.model_dump(mode="json")})
            except KeyError:
                events.put(
                    {
                        "type": "error",
                        "code": "session_not_found",
                        "message": "会话不存在",
                        "provider": "runtime",
                    }
                )
            except ModelProviderError as exc:
                events.put(
                    {"type": "error", **exc.as_dict(), "status_code": exc.status_code}
                )
            except Exception as exc:
                events.put(
                    {
                        "type": "error",
                        "code": type(exc).__name__,
                        "message": str(exc),
                        "provider": "runtime",
                    }
                )
            finally:
                events.put(None)

        threading.Thread(
            target=worker, daemon=True, name="agent-query-resume"
        ).start()
        return _sse(events)

    @application.get("/v1/agent/approvals")
    def list_runtime_approvals(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role,
            {"approver", "admin"},
        )
        items = request.app.state.runtime_governance.list_pending_approvals(
            principal.tenant_id
        )
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    @application.post(
        "/v1/agent/approvals/{approval_id}",
        response_model=RuntimeAgentResponse,
    )
    def decide_runtime_approval(
        approval_id: str,
        payload: ApprovalRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> RuntimeAgentResponse:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role,
            {"approver", "admin"},
        )
        try:
            result = request.app.state.agent_runtime.decide_approval(
                approval_id=approval_id,
                tenant_id=principal.tenant_id,
                decided_by=principal.user_id,
                approved=payload.approved,
                comment=payload.comment,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ModelProviderError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.as_dict(),
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action=(
                "agent_tool.approved"
                if payload.approved else "agent_tool.rejected"
            ),
            resource_type="agent_tool_approval",
            resource_id=approval_id,
            detail={"comment": payload.comment},
        )
        return result

    @application.post("/v1/agent/subagents", status_code=202)
    def create_subagent_task(
        payload: SubagentSubmitRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role,
            {"operator", "admin"},
        )
        parent_events = request.app.state.session_events.list_events(
            session_id=payload.parent_session_id,
            tenant_id=principal.tenant_id,
        )
        if parent_events:
            owned_session_events(request, principal, payload.parent_session_id)
        try:
            task = request.app.state.subagent_manager.submit(
                payload,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                role=principal.role,
            )
            if payload.wait:
                task = request.app.state.subagent_manager.wait(
                    task.task_id,
                    principal.tenant_id,
                    timeout=task.timeout_seconds + 2,
                )
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return task.model_dump(mode="json")

    @application.get("/v1/agent/subagents")
    def list_subagent_tasks(
        request: Request,
        parent_session_id: str | None = Query(default=None),
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        items = request.app.state.subagent_manager.list(
            principal.tenant_id, parent_session_id
        )
        items = [item for item in items if item.user_id == principal.user_id]
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    @application.get("/v1/agent/subagents/{task_id}")
    def get_subagent_task(
        task_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        task = request.app.state.subagent_manager.get(
            task_id, principal.tenant_id
        )
        if task is None:
            raise HTTPException(status_code=404, detail="subagent task not found")
        if task.user_id != principal.user_id:
            raise HTTPException(status_code=404, detail="subagent task not found")
        return task.model_dump(mode="json")

    @application.post("/v1/agent/subagents/{task_id}/cancel")
    def cancel_subagent_task(
        task_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role,
            {"operator", "admin"},
        )
        existing = request.app.state.subagent_manager.get(
            task_id, principal.tenant_id
        )
        if existing is None or existing.user_id != principal.user_id:
            raise HTTPException(status_code=404, detail="subagent task not found")
        try:
            task = request.app.state.subagent_manager.cancel(
                task_id, principal.tenant_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return task.model_dump(mode="json")

    @application.post(
        "/v1/agent/attachments",
        response_model=AttachmentReference,
        status_code=201,
    )
    def upload_agent_attachment(
        payload: AttachmentUploadRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> AttachmentReference:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        try:
            return request.app.state.attachment_store.save(
                payload, tenant_id=principal.tenant_id
            )
        except AttachmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get("/v1/agent/workspace/file")
    def download_workspace_file(
        request: Request,
        path: str = Query(..., min_length=1, max_length=2048),
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> FileResponse:
        principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role,
            {"operator", "admin"},
        )
        try:
            target = request.app.state.sandbox_runner.resolve_workspace_file(path)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="工作区中找不到该文件") from exc
        max_bytes = 16 * 1024 * 1024
        if target.stat().st_size > max_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        media_type, _ = mimetypes.guess_type(target.name)
        if target.suffix.lower() == ".csv":
            from urllib.parse import quote

            body = request.app.state.sandbox_runner.sanitize_csv_bytes(target.read_bytes())
            encoded_name = quote(target.name)
            return Response(
                content=body,
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f"attachment; filename=\"{target.name}\"; "
                        f"filename*=UTF-8''{encoded_name}"
                    ),
                },
            )
        return FileResponse(
            target,
            media_type=media_type or "application/octet-stream",
            filename=target.name,
            content_disposition_type="attachment",
        )

    @application.get("/v1/agent/skills")
    def list_agent_skills(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        items = [
            item.as_dict() for item in request.app.state.skill_registry.list()
        ]
        return {"items": items, "count": len(items)}

    @application.get("/v1/agent/sessions")
    def list_agent_sessions(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        items = request.app.state.session_events.list_sessions(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            limit=limit,
        )
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    @application.get("/v1/agent/sessions/{session_id}/events")
    def agent_session_events(
        session_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        events = owned_session_events(request, principal, session_id)
        return {
            "session_id": session_id,
            "items": [
                sanitize_public_value(event.model_dump(mode="json"))
                for event in events
            ],
            "count": len(events),
        }

    @application.post("/v1/agent/sessions/{session_id}/interrupt")
    def interrupt_agent_session(
        session_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        owned_session_events(request, principal, session_id)
        if not request.app.state.agent_runtime.live_hub.interrupt(session_id):
            raise HTTPException(status_code=409, detail="session is not running")
        request.app.state.session_events.append(
            session_id=session_id,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            event_type="turn.interrupt_requested",
            payload={"status": "interrupt_requested"},
        )
        cancelled = 0
        for task in request.app.state.subagent_manager.list(
            principal.tenant_id, session_id
        ):
            if task.status not in {"queued", "running", "cancel_requested"}:
                continue
            try:
                request.app.state.subagent_manager.cancel(
                    task.task_id, principal.tenant_id
                )
                cancelled += 1
            except KeyError:
                pass
        return {
            "session_id": session_id,
            "status": "interrupt_requested",
            "subagents_cancel_requested": cancelled,
        }

    @application.delete("/v1/agent/sessions/{session_id}")
    def delete_agent_session(
        session_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        owned_session_events(request, principal, session_id)
        tasks = request.app.state.subagent_manager.list(
            principal.tenant_id, session_id
        )
        for task in tasks:
            if task.status in {"queued", "running", "cancel_requested"}:
                try:
                    request.app.state.subagent_manager.cancel(
                        task.task_id, principal.tenant_id
                    )
                except KeyError:
                    pass
        deleted = request.app.state.session_events.delete_session(
            session_id=session_id,
            tenant_id=principal.tenant_id,
        )
        child_deleted = 0
        result_deleted = request.app.state.result_store.delete_session(
            session_id, principal.tenant_id
        )
        for task in tasks:
            child_deleted += request.app.state.session_events.delete_session(
                session_id=task.child_session_id,
                tenant_id=principal.tenant_id,
            )
            result_deleted += request.app.state.result_store.delete_session(
                task.child_session_id, principal.tenant_id
            )
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="agent_session.deleted",
            resource_type="agent_session",
            resource_id=session_id,
            detail={
                "event_count": deleted,
                "child_event_count": child_deleted,
                "subagent_count": len(tasks),
                "result_count": result_deleted,
            },
        )
        return {
            "session_id": session_id,
            "deleted": True,
            "event_count": deleted,
            "child_event_count": child_deleted,
            "result_count": result_deleted,
        }

    @application.get("/v1/agent/results/{result_ref}")
    def get_agent_result(
        result_ref: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        record = request.app.state.result_store.get(
            result_ref, principal.tenant_id
        )
        if record is None:
            raise HTTPException(status_code=404, detail="result not found")
        if record.user_id != principal.user_id:
            raise HTTPException(status_code=404, detail="result not found")
        return sanitize_public_value(result_page(record, offset=offset, limit=limit))

    @application.get("/v1/agent/metrics")
    def agent_runtime_metrics(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        summary = request.app.state.metrics_store.summarize(principal.tenant_id)
        return summary.model_dump(mode="json")

    @application.post(
        "/v1/amazon-finance/query",
        response_model=AmazonFinanceQueryResponse,
    )
    def query_amazon_finance(
        payload: AmazonFinanceQueryRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> AmazonFinanceQueryResponse:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        registry: AgentRegistry = request.app.state.agent_registry
        config = registry.amazon_finance_config()
        if not config.enabled:
            raise HTTPException(status_code=503, detail="Amazon Finance Agent is disabled")
        if not _amazon_finance_active(
            request.app.state.settings,
            registry,
            request.app.state.connection_registry,
            principal.tenant_id,
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "connector_not_configured",
                    "message": "Amazon 财务查询尚未配置可用连接。",
                    "hint": "请管理员在“连接器”页面创建 PostgreSQL 或 MySQL 连接，再在“工具”页面绑定。",
                },
            )
        plan = request.app.state.amazon_finance_agent.plan(payload)
        output = execute_direct_tool(
            request,
            principal,
            tool_name="amazon_finance_query",
            arguments=plan.model_dump(mode="json"),
        )
        result = AmazonFinanceQueryResponse(
            question=payload.question,
            plan=output["plan"],
            columns=output.get("columns", []),
            rows=output.get("rows", []),
            summary=output.get("summary", ""),
            data_scope=output.get("data_scope", "RELEASED only"),
        )
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="amazon_finance.queried",
            resource_type="amazon_finance",
            resource_id="amazon-finance",
            detail={
                "metric": result.plan.metric,
                "start_date": str(result.plan.start_date or ""),
                "end_date": str(result.plan.end_date or ""),
                "row_count": len(result.rows),
            },
        )
        return result

    @application.post(
        "/v1/kingdee-cloud/query",
        response_model=KingdeeQueryResponse,
    )
    def query_kingdee_cloud(
        payload: KingdeeQueryRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> KingdeeQueryResponse:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        registry: AgentRegistry = request.app.state.agent_registry
        config = registry.kingdee_cloud_config()
        if not config.enabled:
            raise HTTPException(status_code=503, detail="Kingdee Cloud Agent is disabled")
        if not _kingdee_cloud_active(
            registry, request.app.state.connection_registry, principal.tenant_id
        ):
            raise HTTPException(
                status_code=503,
                detail="Kingdee integration is not configured in agent settings",
            )
        plan = request.app.state.kingdee_cloud_agent.plan(payload)
        output = execute_direct_tool(
            request,
            principal,
            tool_name="kingdee_cloud_query",
            arguments=plan.model_dump(mode="json"),
        )
        result = KingdeeQueryResponse(
            question=payload.question,
            plan=output["plan"],
            document_label=output["document_label"],
            form_id=output["form_id"],
            columns=output.get("columns", []),
            rows=output.get("rows", []),
            summary=output.get("summary", ""),
            total=output.get("total", 0),
            data_scope=output.get("data_scope", "金蝶云星空 · ExecuteBillQuery"),
        )
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="kingdee_cloud.queried",
            resource_type="kingdee_cloud",
            resource_id=config.id,
            detail={
                "document_type": result.plan.document_type,
                "start_date": str(result.plan.start_date),
                "end_date": str(result.plan.end_date),
                "row_count": len(result.rows),
            },
        )
        return result

    @application.post(
        "/v1/lingxing-profit/query",
        response_model=LingXingProfitQueryResponse,
    )
    def query_lingxing_profit(
        payload: LingXingProfitQueryRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> LingXingProfitQueryResponse:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        registry: AgentRegistry = request.app.state.agent_registry
        config = registry.lingxing_profit_config()
        if not config.enabled:
            raise HTTPException(status_code=503, detail="LingXing Profit Agent is disabled")
        if not _lingxing_profit_active(
            registry, request.app.state.connection_registry, principal.tenant_id
        ):
            raise HTTPException(
                status_code=503,
                detail="LingXing integration is not configured in agent settings",
            )
        plan = request.app.state.lingxing_profit_agent.plan(payload)
        output = execute_direct_tool(
            request,
            principal,
            tool_name="lingxing_profit_query",
            arguments=plan.model_dump(mode="json"),
        )
        result = LingXingProfitQueryResponse(
            question=payload.question,
            plan=output["plan"],
            columns=output.get("columns", []),
            rows=output.get("rows", []),
            summary=output.get("summary", ""),
            total=output.get("total", 0),
            data_scope=output.get(
                "data_scope", "领星利润报表 · 订单维度 transaction 视图"
            ),
        )
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="lingxing_profit.queried",
            resource_type="lingxing_profit",
            resource_id=config.id,
            detail={
                "start_date": str(result.plan.start_date),
                "end_date": str(result.plan.end_date),
                "currency_code": result.plan.currency_code or "native",
                "row_count": len(result.rows),
                "total": result.total,
            },
        )
        return result

    @application.post(
        "/v1/profit-report/query",
        response_model=ProfitReportQueryResponse,
    )
    def query_profit_report(
        payload: ProfitReportQueryRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> ProfitReportQueryResponse:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        registry: AgentRegistry = request.app.state.agent_registry
        config = registry.profit_report_config()
        if not config.enabled:
            raise HTTPException(status_code=503, detail="Profit Report Agent is disabled")
        if not _profit_report_active(
            request.app.state.settings,
            registry,
            request.app.state.connection_registry,
            principal.tenant_id,
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "connector_not_configured",
                    "message": "利润报表查询尚未配置可用连接。",
                    "hint": "请管理员在“连接器”页面创建 PostgreSQL 或 MySQL 连接，再在“工具”页面绑定。",
                },
            )
        plan = request.app.state.profit_report_agent.plan(payload)
        output = execute_direct_tool(
            request,
            principal,
            tool_name="profit_report_query",
            arguments=plan.model_dump(mode="json"),
        )
        result = ProfitReportQueryResponse(
            question=payload.question,
            plan=output["plan"],
            columns=output.get("columns", []),
            rows=output.get("rows", []),
            summary=output.get("summary", ""),
            total_rows=output.get("total_rows", 0),
            data_scope=output.get(
                "data_scope", "领星利润分析数据（分析仓）"
            ),
        )
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="profit_report.queried",
            resource_type="profit_report",
            resource_id=config.id,
            detail={
                "metric": result.plan.metric,
                "start_date": str(result.plan.start_date or ""),
                "end_date": str(result.plan.end_date or ""),
                "currency_code": result.plan.currency_code or "",
                "row_count": len(result.rows),
                "total_rows": result.total_rows,
            },
        )
        return result

    @application.get("/v1/connections")
    def list_connections(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        registry = request.app.state.connection_registry
        health = {
            item["connection_id"]: item
            for item in request.app.state.connector_runtime.health_for_tenant(
                principal.tenant_id
            )
        }
        items = [
            connection_payload(request, item) | {"health": health.get(item.id)}
            for item in registry.list_for_tenant(principal.tenant_id)
        ]
        return {"items": items, "count": len(items)}

    @application.post("/v1/connections", status_code=201)
    def create_connection(
        payload: ConnectionCreateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            connection = request.app.state.connection_registry.create(
                tenant_id=principal.tenant_id,
                connector_type=payload.connector_type,
                name=payload.name,
                enabled=payload.enabled,
                values={**payload.config, **payload.credentials},
                resource_scopes=payload.resource_scopes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="connection.created",
            resource_type="connection",
            resource_id=connection.id,
            detail={"connector_type": connection.connector_type},
        )
        return connection_payload(request, connection)

    @application.get("/v1/connections/health")
    def connection_health(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        items = request.app.state.connector_runtime.health_for_tenant(
            principal.tenant_id
        )
        return {"items": items, "count": len(items)}

    @application.put("/v1/connections/{connector_type}")
    def put_connection(
        connector_type: str,
        payload: ConnectionUpsertRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request,
            x_api_key,
            x_tenant_id,
            x_user_id,
            x_user_role,
            {"admin"},
        )
        if connector_type not in {
            "analytics", "lingxing", "kingdee", "dingtalk", "qdrant", "milvus", "tavily"
        }:
            raise HTTPException(status_code=404, detail="unknown connector type")
        registry = request.app.state.connection_registry
        try:
            connection = registry.upsert(
                tenant_id=principal.tenant_id,
                connector_type=connector_type,
                values={**payload.config, **payload.credentials},
                resource_scopes=payload.resource_scopes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        request.app.state.connector_runtime.invalidate(connection.id)
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="connection.updated",
            resource_type="connection",
            resource_id=connection.id,
            detail={"connector_type": connector_type},
        )
        return connection_payload(request, connection)

    @application.patch("/v1/connections/{connection_id}")
    def patch_connection(
        connection_id: str,
        payload: ConnectionUpdateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            connection = request.app.state.connection_registry.update(
                connection_id, principal.tenant_id, payload
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="connection not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        request.app.state.connector_runtime.invalidate(connection.id)
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="connection.updated",
            resource_type="connection",
            resource_id=connection.id,
            detail={"connector_type": connection.connector_type},
        )
        return connection_payload(request, connection)

    @application.delete("/v1/connections/{connection_id}", status_code=204)
    def delete_connection(
        connection_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        bound_tools = request.app.state.tool_bindings.tools_for_connection(
            principal.tenant_id, connection_id
        )
        if bound_tools:
            raise HTTPException(
                status_code=409,
                detail=f"connection is bound to tools: {', '.join(bound_tools)}",
            )
        knowledge_spaces = request.app.state.knowledge_spaces.spaces_for_connection(
            principal.tenant_id, connection_id
        )
        if knowledge_spaces:
            raise HTTPException(
                status_code=409,
                detail="connection is used by knowledge spaces",
            )
        try:
            connection = request.app.state.connection_registry.delete(
                connection_id, principal.tenant_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="connection not found") from exc
        request.app.state.connector_runtime.invalidate(connection.id)
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="connection.deleted",
            resource_type="connection",
            resource_id=connection.id,
            detail={"connector_type": connection.connector_type},
        )
        return Response(status_code=204)

    @application.get("/v1/knowledge/spaces")
    def list_knowledge_spaces(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        items = request.app.state.knowledge_spaces.list(principal.tenant_id)
        connections = request.app.state.connection_registry
        return {
            "items": [
                item.model_dump(mode="json")
                | {
                    "connector_type": (
                        connection.connector_type if connection else None
                    ),
                    "connection_name": connection.name if connection else None,
                }
                for item in items
                for connection in [connections.get(item.connection_id, principal.tenant_id)]
            ],
            "count": len(items),
        }

    @application.post("/v1/knowledge/spaces", status_code=201)
    def create_knowledge_space(
        payload: KnowledgeSpaceCreate,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            item = request.app.state.knowledge_spaces.create(
                principal.tenant_id, payload
            )
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="knowledge_space.created",
            resource_type="knowledge_space",
            resource_id=item.id,
            detail={"connection_id": item.connection_id},
        )
        return item.model_dump(mode="json")

    @application.patch("/v1/knowledge/spaces/{space_id}")
    def update_knowledge_space(
        space_id: str,
        payload: KnowledgeSpaceUpdate,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            item = request.app.state.knowledge_spaces.update(
                principal.tenant_id, space_id, payload
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="knowledge space not found") from exc
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="knowledge_space.updated",
            resource_type="knowledge_space",
            resource_id=item.id,
            detail={"connection_id": item.connection_id},
        )
        return item.model_dump(mode="json")

    @application.delete("/v1/knowledge/spaces/{space_id}", status_code=204)
    def delete_knowledge_space(
        space_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            item = request.app.state.knowledge_spaces.delete(
                principal.tenant_id, space_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="knowledge space not found") from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="knowledge_space.deleted",
            resource_type="knowledge_space",
            resource_id=item.id,
            detail={},
        )
        return Response(status_code=204)

    @application.post("/v1/knowledge/spaces/{space_id}/test")
    def test_knowledge_space(
        space_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        item = request.app.state.knowledge_spaces.get(principal.tenant_id, space_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge space not found")
        try:
            result = request.app.state.connector_runtime.execute_connection(
                principal.tenant_id,
                item.connection_id,
                lambda client, _connection: client.check_collection(
                    item.collection_name
                ),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"向量数据库连接失败: {exc}",
            ) from exc
        if not result.get("exists"):
            raise HTTPException(
                status_code=404,
                detail=f"Collection 不存在: {item.collection_name}",
            )
        return {"state": "ready", **result}

    @application.get("/v1/knowledge/spaces/{space_id}/contents")
    def list_knowledge_contents(
        space_id: str,
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None),
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        item = request.app.state.knowledge_spaces.get(principal.tenant_id, space_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge space not found")
        try:
            result = request.app.state.connector_runtime.execute_connection(
                principal.tenant_id,
                item.connection_id,
                lambda client, _connection: client.list_contents(
                    item.collection_name,
                    limit=limit,
                    cursor=cursor,
                    text_field=item.text_field,
                    category_field=item.category_field,
                ),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"读取知识库内容失败: {exc}",
            ) from exc
        contents = list(result.get("items") or [])
        categories = sorted(
            {
                str(content.get("category") or "未分类")
                for content in contents
            }
        )
        return {
            "space_id": item.id,
            "space_name": item.name,
            "collection": item.collection_name,
            "items": contents,
            "count": len(contents),
            "total": int(result.get("total") or 0),
            "categories": categories,
            "next_cursor": result.get("next_cursor"),
        }

    @application.get("/v1/tool-bindings")
    def list_tool_bindings(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        items = request.app.state.tool_bindings.catalog(
            principal.tenant_id, request.app.state.connection_registry
        )
        return {"items": items, "count": len(items)}

    @application.put("/v1/tools/{tool_name}/connection")
    def bind_tool_connection(
        tool_name: str,
        payload: ToolConnectionBindingRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            connection = request.app.state.tool_bindings.select(
                principal.tenant_id,
                tool_name,
                payload.connection_id,
                request.app.state.connection_registry,
                payload.resource_scopes,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="tool.connection_bound",
            resource_type="tool",
            resource_id=tool_name,
            detail={
                "connection_id": connection.id,
                "resource_scopes": payload.resource_scopes,
            },
        )
        return {
            "tool_name": tool_name,
            "connection_id": connection.id,
            "connector_type": connection.connector_type,
            "resource_scopes": request.app.state.tool_bindings.selected_resource_scopes(
                principal.tenant_id, tool_name, request.app.state.connection_registry
            ),
        }

    def _memory_service(request: Request):
        service = request.app.state.memory_service
        if service is None:
            raise HTTPException(status_code=503, detail="memory service is disabled")
        return service

    @application.get("/v1/memories")
    def list_memories(
        request: Request,
        status: str | None = Query(default=None),
        scope: str | None = Query(default=None),
        owner_user_id: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        include_deleted: bool = Query(default=False),
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        items = _memory_service(request).list(
            principal.tenant_id,
            user_id=owner_user_id,
            status=status,
            scope=scope,
            agent_id=agent_id,
            include_deleted=include_deleted,
        )
        return {"items": [item.model_dump(mode="json") for item in items], "count": len(items)}

    @application.post("/v1/memories", status_code=201)
    def create_memory(
        payload: MemoryAdminCreateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        data = payload.model_dump(exclude={"owner_user_id"})
        item = _memory_service(request).create(
            MemoryCreate.model_validate(data),
            tenant_id=principal.tenant_id,
            user_id=payload.owner_user_id or principal.user_id,
            source="admin",
        )
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="memory.created",
            resource_type="memory",
            resource_id=item.id,
            detail={"scope": item.scope, "owner_user_id": item.user_id},
        )
        return item.model_dump(mode="json")

    @application.get("/v1/memories/search")
    def search_memories(
        request: Request,
        query: str = Query(min_length=1, max_length=2000),
        owner_user_id: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=50),
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        items = _memory_service(request).search(
            query,
            tenant_id=principal.tenant_id,
            user_id=owner_user_id or principal.user_id,
            agent_id=agent_id,
            limit=limit,
        )
        return {"items": items, "count": len(items)}

    @application.get("/v1/memories/profiles/{managed_user_id}")
    def memory_profile(
        managed_user_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        return _memory_service(request).profile(principal.tenant_id, managed_user_id)

    @application.post("/v1/memories/{memory_id}/confirm")
    def confirm_memory(
        memory_id: str,
        payload: MemoryDecisionRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            item = _memory_service(request).confirm(
                principal.tenant_id, memory_id, replace_conflicts=payload.replace_conflicts
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id, actor_id=principal.user_id,
            actor_role=principal.role, action="memory.confirmed",
            resource_type="memory", resource_id=memory_id,
            detail={"replace_conflicts": payload.replace_conflicts},
        )
        return item.model_dump(mode="json")

    @application.post("/v1/memories/{memory_id}/reject")
    def reject_memory(
        memory_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            item = _memory_service(request).reject(principal.tenant_id, memory_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id, actor_id=principal.user_id,
            actor_role=principal.role, action="memory.rejected",
            resource_type="memory", resource_id=memory_id, detail={},
        )
        return item.model_dump(mode="json")

    @application.post("/v1/memories/{memory_id}/correct", status_code=201)
    def correct_memory(
        memory_id: str,
        payload: MemoryCorrectionRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            item = _memory_service(request).correct(
                principal.tenant_id, memory_id, payload.content, principal.user_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id, actor_id=principal.user_id,
            actor_role=principal.role, action="memory.corrected",
            resource_type="memory", resource_id=item.id,
            detail={"correction_of": memory_id},
        )
        return item.model_dump(mode="json")

    @application.delete("/v1/memories/{memory_id}", status_code=204)
    def delete_memory(
        memory_id: str,
        payload: MemoryDeleteRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        if not _memory_service(request).forget(
            principal.tenant_id, memory_id, reason=payload.reason
        ):
            raise HTTPException(status_code=404, detail="memory not found")
        request.app.state.store.audit(
            tenant_id=principal.tenant_id, actor_id=principal.user_id,
            actor_role=principal.role, action="memory.forgotten",
            resource_type="memory", resource_id=memory_id,
            detail={"reason": payload.reason},
        )
        return Response(status_code=204)

    @application.delete("/v1/memories/users/{managed_user_id}/compliance", status_code=204)
    def compliance_delete_memories(
        managed_user_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        count = _memory_service(request).compliance_delete_user(
            principal.tenant_id, managed_user_id
        )
        request.app.state.store.audit(
            tenant_id=principal.tenant_id, actor_id=principal.user_id,
            actor_role=principal.role, action="memory.compliance_deleted",
            resource_type="user_memory", resource_id=managed_user_id,
            detail={"deleted_count": count},
        )
        return Response(status_code=204)

    @application.get("/v1/access-control")
    def access_control_snapshot(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        result = request.app.state.access_control.snapshot(principal.tenant_id)
        accounts = {
            item["user_id"]: item
            for item in request.app.state.account_service.list_accounts(principal.tenant_id)
        }
        for user in result["users"]:
            user["account"] = accounts.get(user["id"])
        result["tool_catalog"] = [
            {"id": tool["id"], "name": tool["name"]}
            for tool in request.app.state.runtime_tool_registry.catalog()
            if tool["id"] not in SYSTEM_DEFAULT_TOOL_NAMES
        ]
        result["default_tool_names"] = sorted(SYSTEM_DEFAULT_TOOL_NAMES)
        return result

    @application.put("/v1/access-control/users/{managed_user_id}")
    def put_access_user(
        managed_user_id: str,
        payload: AccessUserUpsert,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        if payload.id != managed_user_id:
            raise HTTPException(status_code=400, detail="payload id must match path")
        account = request.app.state.account_service.account(
            principal.tenant_id, managed_user_id
        )
        generated_password = None
        if account or payload.temporary_password or payload.generate_temporary_password:
            try:
                account, generated_password = request.app.state.account_service.provision(
                    principal.tenant_id, managed_user_id, payload.name, payload.role,
                    payload.enabled, payload.temporary_password,
                    payload.generate_temporary_password,
                )
            except AccountError as error:
                raise account_failure(error) from error
        result = request.app.state.access_control.put_user(
            principal.tenant_id, managed_user_id, payload.name, payload.enabled
        )
        result["account"] = account
        if generated_password:
            result["temporary_password"] = generated_password
        request.app.state.store.audit(
            tenant_id=principal.tenant_id, actor_id=principal.user_id,
            actor_role=principal.role, action="access.user_saved",
            resource_type="access_user", resource_id=managed_user_id,
            detail={"enabled": payload.enabled, "role": payload.role},
        )
        return result

    @application.delete("/v1/access-control/users/{managed_user_id}", status_code=204)
    def delete_access_user(
        managed_user_id: str, request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        if not request.app.state.access_control.delete_user(
            principal.tenant_id, managed_user_id
        ):
            raise HTTPException(status_code=404, detail="user not found")
        request.app.state.account_service.store.delete(principal.tenant_id, managed_user_id)
        return Response(status_code=204)

    @application.post("/v1/access-control/users/{managed_user_id}/reset-password")
    def reset_access_user_password(
        managed_user_id: str,
        payload: ResetPasswordRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        try:
            account, generated = request.app.state.account_service.reset_password(
                principal.tenant_id, managed_user_id, payload.temporary_password,
                payload.generate_temporary_password,
            )
        except AccountError as error:
            raise account_failure(error) from error
        request.app.state.store.audit(
            tenant_id=principal.tenant_id, actor_id=principal.user_id,
            actor_role=principal.role, action="account.password_reset",
            resource_type="account", resource_id=managed_user_id, detail={},
        )
        result = {"account": account}
        if generated:
            result["temporary_password"] = generated
        return result

    @application.post("/v1/access-control/groups", status_code=201)
    def create_permission_group(
        payload: PermissionGroupUpsert, request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        item_id = payload.id or request.app.state.access_control.new_id("group")
        return request.app.state.access_control.put_group(
            principal.tenant_id, item_id, payload.name, payload.description
        )

    @application.put("/v1/access-control/groups/{group_id}")
    def update_permission_group(
        group_id: str,
        payload: PermissionGroupUpsert,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        store = request.app.state.access_control
        current = store.get_group(principal.tenant_id, group_id)
        if current is None:
            raise HTTPException(status_code=404, detail="group not found")
        updated = store.put_group(
            principal.tenant_id,
            group_id,
            payload.name.strip(),
            payload.description.strip(),
        )
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="access.permission_group_updated",
            resource_type="permission_group",
            resource_id=group_id,
            detail={"old_name": current["name"], "new_name": updated["name"]},
        )
        return updated

    @application.put("/v1/access-control/groups/{group_id}/tools")
    def update_permission_group_tools(
        group_id: str,
        payload: PermissionGroupToolsUpdate,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        store = request.app.state.access_control
        if not store.get_group(principal.tenant_id, group_id):
            raise HTTPException(status_code=404, detail="group not found")
        known = {
            item["id"] for item in request.app.state.runtime_tool_registry.catalog()
        }
        unknown = sorted(set(payload.tool_names) - known)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "unknown_tools",
                    "message": "包含未注册的 Tool。",
                    "tool_names": unknown,
                },
            )
        system_tools = sorted(set(payload.tool_names) & SYSTEM_DEFAULT_TOOL_NAMES)
        if system_tools:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "system_tool_not_configurable",
                    "message": "系统基础 Tool 默认授予所有已启用用户，无需在权限组中配置。",
                    "tool_names": system_tools,
                },
            )
        return store.set_group_tools(
            principal.tenant_id, group_id, payload.tool_names
        )

    @application.delete("/v1/access-control/groups/{group_id}", status_code=204)
    def delete_permission_group(
        group_id: str, request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        if not request.app.state.access_control.delete_group(principal.tenant_id, group_id):
            raise HTTPException(status_code=404, detail="group not found")
        return Response(status_code=204)

    @application.post("/v1/access-control/rules", status_code=201)
    def create_permission_rule(
        payload: PermissionRuleUpsert, request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        known = {
            item["id"] for item in request.app.state.runtime_tool_registry.catalog()
        }
        unknown = sorted(set(payload.tool_names) - known)
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown tools: {unknown}")
        system_tools = sorted(set(payload.tool_names) & SYSTEM_DEFAULT_TOOL_NAMES)
        if system_tools:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "system_tool_not_configurable",
                    "message": "系统基础 Tool 默认授予所有已启用用户，无需加入权限规则。",
                    "tool_names": system_tools,
                },
            )
        store = request.app.state.access_control
        if not store.get_group(
            principal.tenant_id, payload.group_id
        ):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "permission_group_not_found",
                    "message": "所属权限组不存在。",
                },
            )
        item_id = payload.id or store.new_id("rule")
        owners = {
            tool_name: {"rule_id": rule["id"], "rule_name": rule["name"]}
            for rule in store.list_rules(principal.tenant_id)
            if rule["id"] != item_id and rule["group_id"] == payload.group_id
            for tool_name in rule["tool_names"]
        }
        conflicts = [
            {"tool_name": tool_name, **owners[tool_name]}
            for tool_name in sorted(set(payload.tool_names) & owners.keys())
        ]
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "tool_already_assigned",
                    "message": "该权限组内的其他规则已包含部分 Tool。",
                    "hint": "请从同一权限组的原规则中移除对应 Tool。其他权限组仍可使用这些 Tool。",
                    "conflicts": conflicts,
                },
            )
        try:
            return store.put_rule(
                principal.tenant_id, item_id, payload.name,
                payload.tool_names, payload.description, payload.group_id,
            )
        except ToolAssignmentConflict as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "tool_already_assigned",
                    "message": "该权限组内的其他规则刚刚包含了相同 Tool，请刷新后重试。",
                },
            ) from error

    @application.delete("/v1/access-control/rules/{rule_id}", status_code=204)
    def delete_permission_rule(
        rule_id: str, request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        if not request.app.state.access_control.delete_rule(principal.tenant_id, rule_id):
            raise HTTPException(status_code=404, detail="rule not found")
        return Response(status_code=204)

    @application.put("/v1/access-control/users/{managed_user_id}/groups")
    def bind_user_group(
        managed_user_id: str, payload: AccessBindingRequest, request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        store = request.app.state.access_control
        if not store.get_user(principal.tenant_id, managed_user_id) or not store.get_group(principal.tenant_id, payload.target_id):
            raise HTTPException(status_code=404, detail="user or group not found")
        store.bind_user_group(principal.tenant_id, managed_user_id, payload.target_id)
        return store.get_user(principal.tenant_id, managed_user_id)

    @application.delete("/v1/access-control/users/{managed_user_id}/groups/{group_id}", status_code=204)
    def unbind_user_group(
        managed_user_id: str, group_id: str, request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        request.app.state.access_control.unbind_user_group(
            principal.tenant_id, managed_user_id, group_id
        )
        return Response(status_code=204)

    @application.put("/v1/access-control/groups/{group_id}/rules")
    def bind_group_rule(
        group_id: str, payload: AccessBindingRequest, request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        store = request.app.state.access_control
        if not store.get_group(principal.tenant_id, group_id) or not store.get_rule(principal.tenant_id, payload.target_id):
            raise HTTPException(status_code=404, detail="group or rule not found")
        try:
            store.bind_group_rule(principal.tenant_id, group_id, payload.target_id)
        except ToolAssignmentConflict as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "tool_already_assigned",
                    "message": "无法移动规则：目标权限组已包含相同 Tool。",
                    "hint": "请先调整目标权限组中的规则。",
                },
            ) from error
        return store.get_group(principal.tenant_id, group_id)

    @application.delete("/v1/access-control/groups/{group_id}/rules/{rule_id}", status_code=204)
    def unbind_group_rule(
        group_id: str, rule_id: str, request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role, {"admin"}
        )
        request.app.state.access_control.unbind_group_rule(
            principal.tenant_id, group_id, rule_id
        )
        return Response(status_code=204)

    @application.get("/v1/agents")
    def list_agents(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        registry: AgentRegistry = request.app.state.agent_registry
        settings = request.app.state.settings
        decision = request.app.state.access_control.effective_access(
            principal.tenant_id, principal.user_id, principal.role
        )
        items = []
        for agent in registry.list():
            if not agent_visible_for_access(agent, decision.allowed_tools):
                continue
            payload = mask_agent_integration(agent)
            if decision.allowed_tools is not None:
                payload["allowed_tools"] = [
                    name for name in agent.allowed_tools
                    if name in decision.allowed_tools
                ]
            connector_type = {
                "lingxing-profit-report": "lingxing",
                "kingdee-cloud": "kingdee",
            }.get(agent.id)
            if connector_type:
                connection = request.app.state.connection_registry.get_default(
                    principal.tenant_id, connector_type
                )
                if connection is not None:
                    payload["integration"] = (
                        request.app.state.connection_registry.masked_values(connection)
                    )
            payload["status"] = "active"
            if not agent.enabled:
                payload["status"] = "disabled"
            elif agent.id == "amazon-finance-query" and not _amazon_finance_active(
                settings,
                registry,
                request.app.state.connection_registry,
                principal.tenant_id,
            ):
                payload["status"] = "disabled"
            elif agent.id == "lingxing-profit-report" and not _lingxing_profit_active(
                registry, request.app.state.connection_registry, principal.tenant_id
            ):
                payload["status"] = "disabled"
            elif agent.id == "profit-report-query" and not _profit_report_active(
                settings,
                registry,
                request.app.state.connection_registry,
                principal.tenant_id,
            ):
                payload["status"] = "disabled"
            elif agent.id == "kingdee-cloud" and not _kingdee_cloud_active(
                registry, request.app.state.connection_registry, principal.tenant_id
            ):
                payload["status"] = "disabled"
            items.append(payload)
        return {"items": items, "count": len(items)}

    @application.get("/v1/agents/{agent_id}")
    def get_agent(
        agent_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        registry: AgentRegistry = request.app.state.agent_registry
        agent = registry.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        decision = request.app.state.access_control.effective_access(
            principal.tenant_id, principal.user_id, principal.role
        )
        if not agent_visible_for_access(agent, decision.allowed_tools):
            raise HTTPException(status_code=404, detail="agent not found")
        payload = mask_agent_integration(agent)
        if decision.allowed_tools is not None:
            payload["allowed_tools"] = [
                name for name in agent.allowed_tools
                if name in decision.allowed_tools
            ]
        connector_type = {
            "lingxing-profit-report": "lingxing",
            "kingdee-cloud": "kingdee",
        }.get(agent.id)
        if connector_type:
            connection = request.app.state.connection_registry.get_default(
                principal.tenant_id, connector_type
            )
            if connection is not None:
                payload["integration"] = (
                    request.app.state.connection_registry.masked_values(connection)
                )
        if agent.id in {COORDINATOR_AGENT_ID, ANALYST_AGENT_ID} or agent.id in SPECIALIST_ANALYST_IDS:
            tool_context = ToolExecutionContext(
                session_id="agent-detail",
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                role=principal.role,
                allowed_tool_names=decision.allowed_tools,
            )
            tool_catalog = request.app.state.runtime_tool_registry.catalog_for(
                tool_context
            )
            payload["tool_catalog"] = tool_catalog
            payload["role_tools"] = [
                name for name in agent.allowed_tools
                if decision.allowed_tools is None or name in decision.allowed_tools
            ]
            payload["strict_tool_allowlist"] = agent.strict_tool_allowlist
        return payload

    @application.patch("/v1/agents/{agent_id}")
    def patch_agent(
        agent_id: str,
        payload: AgentUpdateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request,
            x_api_key,
            x_tenant_id,
            x_user_id,
            x_user_role,
            {"admin"},
        )
        registry: AgentRegistry = request.app.state.agent_registry
        decision_agents = {
            COORDINATOR_AGENT_ID,
            ANALYST_AGENT_ID,
            *SPECIALIST_ANALYST_IDS,
        }
        if payload.allowed_tools is not None and agent_id in decision_agents:
            catalog = {
                item["name"]
                for item in request.app.state.runtime_tool_registry.catalog()
            }
            unknown = sorted(set(payload.allowed_tools) - catalog)
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown tools: {', '.join(unknown)}",
                )
            if agent_id == COORDINATOR_AGENT_ID:
                forbidden = sorted(
                    set(payload.allowed_tools) & DATA_QUERY_TOOL_NAMES
                )
                if forbidden:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Coordinator 不能配置数据查询工具，请委派 Analyst: "
                            + ", ".join(forbidden)
                        ),
                    )
            if (
                agent_id == ANALYST_AGENT_ID or agent_id in SPECIALIST_ANALYST_IDS
            ) and "delegate_subagent" in payload.allowed_tools:
                raise HTTPException(
                    status_code=400,
                    detail="Analyst 不能配置 delegate_subagent",
                )
        update_values = payload.model_dump(exclude_unset=True)
        integration_patch = update_values.pop("integration", None)
        if integration_patch is not None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "connector_page_required",
                    "message": "数据连接只能在连接器页面配置。",
                    "hint": "请前往“连接器”新建或编辑连接，再在工具页绑定。",
                },
            )
        safe_payload = AgentUpdateRequest.model_validate(update_values)
        try:
            updated = registry.update(agent_id, safe_payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="agent not found") from exc
        if agent_id == "amazon-finance-query":
            _sync_amazon_finance_agent(
                request.app.state.amazon_finance_agent,
                registry,
            )
        if agent_id == "lingxing-profit-report":
            _sync_lingxing_profit_agent(
                request.app.state.lingxing_profit_agent,
                registry,
            )
        if agent_id == "profit-report-query":
            _sync_profit_report_agent(
                request.app.state.profit_report_agent,
                registry,
            )
        if agent_id == "kingdee-cloud":
            _sync_kingdee_cloud_agent(
                request.app.state.kingdee_cloud_agent,
                registry,
            )
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="agent.updated",
            resource_type="agent",
            resource_id=agent_id,
            detail={
                key: value
                for key, value in payload.model_dump(exclude_unset=True).items()
                if key not in {"system_prompt", "integration"}
            },
        )
        return mask_agent_integration(updated)

    @application.get("/v1/models")
    def list_chat_models(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        registry: ModelRegistry = request.app.state.model_registry
        callable_models = [
            model for model in registry.list(enabled_only=True) if model.callable()
        ]
        items = [
            {
                "id": model.id,
                "name": model.name,
                "provider": model.provider,
                "model_name": model.model_name,
                "is_default": model.is_default,
                "supports_vision": model.supports_image_input,
                "supports_image": model.supports_image_input,
                "supports_audio": model.supports_audio_input,
            }
            for model in callable_models
        ]
        default_model_id = next(
            (model.id for model in callable_models if model.is_default),
            callable_models[0].id if callable_models else None,
        )
        return {
            "items": items,
            "count": len(items),
            "default_model_id": default_model_id,
        }

    @application.get("/v1/configuration/models")
    def list_configuration_models(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal_from_headers(
            request,
            x_api_key,
            x_tenant_id,
            x_user_id,
            x_user_role,
            {"admin"},
        )
        registry: ModelRegistry = request.app.state.model_registry
        items = registry.catalog_items()
        callable_models = [
            model for model in registry.list(enabled_only=True) if model.callable()
        ]
        default_model = next(
            (model for model in callable_models if model.is_default),
            callable_models[0] if callable_models else None,
        )
        return {
            "items": items,
            "count": len(items),
            "default_model_id": default_model.id if default_model else None,
        }

    @application.post("/v1/configuration/models", status_code=201)
    def create_configuration_model(
        payload: ModelCreateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request,
            x_api_key,
            x_tenant_id,
            x_user_id,
            x_user_role,
            {"admin"},
        )
        registry: ModelRegistry = request.app.state.model_registry
        try:
            created = registry.create(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _reload_model_router(request.app)
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="model.created",
            resource_type="model",
            resource_id=created.id,
            detail={"name": created.name, "provider": created.provider},
        )
        return mask_model_definition(created)

    @application.patch("/v1/configuration/models/{model_id}")
    def patch_configuration_model(
        model_id: str,
        payload: ModelUpdateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request,
            x_api_key,
            x_tenant_id,
            x_user_id,
            x_user_role,
            {"admin"},
        )
        registry: ModelRegistry = request.app.state.model_registry
        try:
            updated = registry.update(model_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="model not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _reload_model_router(request.app)
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="model.updated",
            resource_type="model",
            resource_id=model_id,
            detail={
                key: value
                for key, value in payload.model_dump(exclude_unset=True).items()
                if key != "api_key"
            },
        )
        return mask_model_definition(updated)

    @application.delete("/v1/configuration/models/{model_id}", status_code=204)
    def delete_configuration_model(
        model_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> Response:
        principal = principal_from_headers(
            request,
            x_api_key,
            x_tenant_id,
            x_user_id,
            x_user_role,
            {"admin"},
        )
        registry: ModelRegistry = request.app.state.model_registry
        try:
            registry.delete(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="model not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _reload_model_router(request.app)
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="model.deleted",
            resource_type="model",
            resource_id=model_id,
            detail={},
        )
        return Response(status_code=204)

    @application.get("/v1/catalog")
    def catalog(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        decision = request.app.state.access_control.effective_access(
            principal.tenant_id, principal.user_id, principal.role
        )
        tool_context = ToolExecutionContext(
            session_id="catalog",
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            role=principal.role,
            allowed_tool_names=decision.allowed_tools,
        )
        runtime_tools = request.app.state.runtime_tool_registry.catalog_for(tool_context)
        agent_snapshot = snapshot_agents(
            request.app.state.agent_registry,
            amazon_active=_amazon_finance_active(
                request.app.state.settings,
                request.app.state.agent_registry,
                request.app.state.connection_registry,
                principal.tenant_id,
            ),
            lingxing_active=_lingxing_profit_active(
                request.app.state.agent_registry,
                request.app.state.connection_registry,
                principal.tenant_id,
            ),
            profit_report_active=_profit_report_active(
                request.app.state.settings,
                request.app.state.agent_registry,
                request.app.state.connection_registry,
                principal.tenant_id,
            ),
            kingdee_active=_kingdee_cloud_active(
                request.app.state.agent_registry,
                request.app.state.connection_registry,
                principal.tenant_id,
            ),
        )
        tool_bindings = request.app.state.tool_bindings.catalog(
            principal.tenant_id, request.app.state.connection_registry
        )
        if decision.allowed_tools is not None:
            tool_bindings = [
                item
                for item in tool_bindings
                if item["tool_name"] in decision.allowed_tools
            ]
        return {
            **agent_snapshot,
            "tools": runtime_tools,
            "tool_bindings": tool_bindings,
        }

    @application.get("/v1/configuration")
    def configuration(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        settings = request.app.state.settings
        model_registry: ModelRegistry = request.app.state.model_registry
        configured_models = model_registry.list()
        callable_models = [
            model for model in configured_models if model.callable()
        ]
        default_model = next(
            (model for model in callable_models if model.is_default),
            callable_models[0] if callable_models else None,
        )
        return {
            "environment": settings.app_env,
            "persistence": {
                "control_plane": settings.control_plane_backend,
                "session_events": settings.session_event_backend,
            },
            "model": {
                "configured": default_model is not None,
                "provider": default_model.provider if default_model else None,
                "name": default_model.model_name if default_model else None,
                "default_model_id": default_model.id if default_model else None,
                "timeout_seconds": settings.model_request_timeout_seconds,
                "max_retries": settings.model_max_retries,
                "backoff_base_seconds": settings.model_backoff_base_seconds,
            },
            "models": {
                "items": model_registry.catalog_items(),
                "count": len(configured_models),
                "default_model_id": default_model.id if default_model else None,
                "configured": default_model is not None,
            },
            "knowledge": {
                "configured": bool(
                    request.app.state.knowledge_gateway.configured
                    or request.app.state.knowledge_spaces.list(principal.tenant_id)
                ),
                "library": request.app.state.knowledge_gateway.status(),
                "spaces": [
                    item.model_dump(mode="json")
                    for item in request.app.state.knowledge_spaces.list(
                        principal.tenant_id
                    )
                ],
                "count": len(
                    request.app.state.knowledge_spaces.list(principal.tenant_id)
                ),
            },
            "amazon_finance": {
                "configured": _amazon_finance_active(
                    settings,
                    request.app.state.agent_registry,
                    request.app.state.connection_registry,
                    principal.tenant_id,
                ),
                "data_scope": "RELEASED only",
                "statement_timeout_ms": settings.analytics_statement_timeout_ms,
            },
            "lingxing_profit": {
                "configured": _lingxing_profit_active(
                    request.app.state.agent_registry,
                    request.app.state.connection_registry,
                    principal.tenant_id,
                ),
                "endpoint": (
                    "/basicOpen/finance/profitReport/order/transcation/list"
                ),
                "credential_source": "tenant_connection",
            },
            "profit_report": {
                "configured": _profit_report_active(
                    request.app.state.settings,
                    request.app.state.agent_registry,
                    request.app.state.connection_registry,
                    principal.tenant_id,
                ),
                "data_source": "领星利润分析数据（分析仓）",
                "import_script": "scripts/import_lingxing_profit_xlsx.py",
            },
            "kingdee_cloud": {
                "configured": _kingdee_cloud_active(
                    request.app.state.agent_registry,
                    request.app.state.connection_registry,
                    principal.tenant_id,
                ),
                "method": "DynamicFormService.ExecuteBillQuery",
                "documents": [
                    "SAL_SaleOrder",
                    "SAL_OUTSTOCK",
                    "AR_receivable",
                    "AR_OtherRecAble",
                ],
                "credential_source": "tenant_connection",
            },
            "agent_runtime": {
                "function_calling": True,
                "tools": request.app.state.runtime_tool_registry.catalog(),
                "skills": [
                    item.as_dict()
                    for item in request.app.state.skill_registry.list()
                ],
                "mcp_servers": request.app.state.mcp_manager.catalog(),
                "attachments": {
                    "image_types": [
                        "image/png", "image/jpeg", "image/webp", "image/gif"
                    ],
                    "max_image_bytes": settings.attachment_max_image_bytes,
                    "max_images_per_message": (
                        settings.attachment_max_images_per_message
                    ),
                    "vision_model": (
                        settings.zhipu_vision_model_name
                        if settings.model_provider == "zhipu"
                        else None
                    ),
                },
                "governance": {
                    "subagent_queue_backend": settings.subagent_queue_backend,
                    "subagent_workers": settings.subagent_worker_count,
                    "subagent_max_depth": settings.subagent_max_depth,
                    "subagent_timeout_seconds": (
                        settings.subagent_default_timeout_seconds
                    ),
                    "subagent_token_budget": (
                        settings.subagent_default_token_budget
                    ),
                    "subagent_lease_seconds": settings.subagent_lease_seconds,
                    "subagent_max_attempts": settings.subagent_max_attempts,
                    "sandbox_restricted_available": (
                        request.app.state.sandbox_runner.restricted_available
                    ),
                    "sandbox_workspace_root": str(
                        settings.sandbox_workspace_root
                    ),
                    "per_call_approval": True,
                },
                "observability": {
                    "otel_exporter": settings.otel_exporter,
                    "jwt_required": settings.jwt_required,
                    "metrics": True,
                },
            },
            "limits": {
                "max_tool_steps": settings.max_tool_steps,
                "run_token_budget": settings.run_token_budget,
            },
            "analyst_runtime": analyst_runtime_snapshot(settings),
            "context_window": context_window_snapshot(settings),
            "secrets": {"exposed": False},
        }

    @application.patch("/v1/configuration/context-window")
    def patch_context_window(
        payload: ContextWindowUpdate,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role,
            {"admin"},
        )
        try:
            snapshot = update_context_window(
                request.app.state.settings,
                payload.model_dump(exclude_none=True),
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"context_window": snapshot}

    @application.patch("/v1/configuration/analyst-runtime")
    def patch_analyst_runtime(
        payload: AnalystRuntimeUpdate,
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request,
            x_api_key,
            x_tenant_id,
            x_user_id,
            x_user_role,
            {"admin"},
        )
        try:
            snapshot = update_analyst_runtime(
                request.app.state.settings,
                payload.model_dump(),
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="analyst_runtime.mode_updated",
            resource_type="runtime_configuration",
            resource_id="analyst-runtime",
            detail=snapshot,
        )
        return {"analyst_runtime": snapshot}

    @application.get("/v1/audit-events")
    def audit_events(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal = principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role,
            {"admin"},
        )
        items = request.app.state.store.list_audit(
            tenant_id=principal.tenant_id, limit=limit
        )
        return {"items": items, "count": len(items)}

    register_knowledge_library_routes(application, principal_from_headers)

    frontend_dir = Path(__file__).resolve().parents[3] / "frontend"
    if frontend_dir.exists():
        application.mount("/ui", StaticFiles(directory=frontend_dir), name="ui")

        @application.get("/", include_in_schema=False)
        def console() -> FileResponse:
            return FileResponse(frontend_dir / "index.html")

        @application.get("/favicon.ico", include_in_schema=False)
        def favicon() -> FileResponse:
            ico = frontend_dir / "favicon.ico"
            return FileResponse(ico if ico.exists() else frontend_dir / "favicon.svg")

    return application


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "ops_agent.api.app:app", host=settings.app_host, port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
