from __future__ import annotations

import json
import logging
import mimetypes
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Any, Iterator

from pydantic import ValidationError
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..agent_integration import mask_agent_integration
from ..runtime.agent_tool_policy import (
    amazon_finance_tool_active,
    kingdee_cloud_tool_active,
    lingxing_profit_tool_active,
    profit_report_tool_active,
)
from ..agent_registry import AgentRegistry, AgentUpdateRequest, snapshot_agents
from ..config import Settings, context_window_snapshot, get_settings, update_context_window
from ..domain import ApprovalRequest
from ..infrastructure.platform_store import PlatformStore, create_platform_store
from ..model_gateway import create_model_gateway
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
)
from ..runtime.auth import principal_from_bearer
from ..runtime.model_errors import ModelProviderError
from ..runtime.model_router import create_model_router_from_registry
from ..runtime.stack import open_runtime_stack
from ..runtime.subagents import SubagentSubmitRequest
from ..runtime.tools import ToolExecutionContext
from ..workflows.amazon_finance.agent import AmazonFinanceAgent, SYSTEM_PROMPT as AMAZON_SYSTEM_PROMPT
from ..workflows.amazon_finance.domain import (
    AmazonFinanceQueryRequest,
    AmazonFinanceQueryResponse,
)
from ..workflows.amazon_finance.query_tool import (
    AmazonFinanceQueryError,
    AmazonFinanceQueryTool,
)
from ..workflows.kingdee_cloud.agent import (
    KingdeeCloudAgent,
    SYSTEM_PROMPT as KINGDEE_SYSTEM_PROMPT,
)
from ..workflows.kingdee_cloud.domain import (
    KingdeeIntegrationConfig,
    KingdeeQueryRequest,
    KingdeeQueryResponse,
)
from ..workflows.kingdee_cloud.query_tool import KingdeeQueryError, KingdeeQueryTool
from ..workflows.lingxing_profit.agent import (
    LingXingProfitAgent,
    SYSTEM_PROMPT as LINGXING_SYSTEM_PROMPT,
)
from ..workflows.lingxing_profit.domain import (
    LingXingIntegrationConfig,
    LingXingProfitQueryRequest,
    LingXingProfitQueryResponse,
)
from ..workflows.lingxing_profit.query_tool import (
    LingXingProfitQueryError,
    LingXingProfitQueryTool,
)
from ..workflows.profit_report.agent import (
    ProfitReportAgent,
    SYSTEM_PROMPT as PROFIT_REPORT_SYSTEM_PROMPT,
)
from ..workflows.profit_report.domain import (
    ProfitReportQueryRequest,
    ProfitReportQueryResponse,
)
from ..workflows.profit_report.query_tool import (
    ProfitReportQueryError,
    ProfitReportQueryTool,
)


LOGGER = logging.getLogger(__name__)
ROLES = {"viewer", "operator", "approver", "admin"}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    role: str


def _amazon_finance_active(settings: Settings, registry: AgentRegistry) -> bool:
    return amazon_finance_tool_active(registry, settings)


def _lingxing_profit_active(registry: AgentRegistry) -> bool:
    return lingxing_profit_tool_active(registry)


def _profit_report_active(settings: Settings, registry: AgentRegistry) -> bool:
    return profit_report_tool_active(registry, settings)


def _kingdee_cloud_active(registry: AgentRegistry) -> bool:
    return kingdee_cloud_tool_active(registry)


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
        application.state.amazon_finance_agent = AmazonFinanceAgent(
            model,
            AmazonFinanceQueryTool(
                runtime_settings.analytics_dsn,
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
                runtime_settings.analytics_dsn,
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
            application.state.runtime_tool_registry = stack.tool_registry
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
        if bearer.lower().startswith("bearer ") and settings.jwt_secret:
            claims = principal_from_bearer(bearer.split(" ", 1)[1].strip(), settings)
            resolved_role = claims["role"]
            if resolved_role not in ROLES:
                raise HTTPException(status_code=400, detail="invalid role")
            if allowed_roles and resolved_role not in allowed_roles:
                raise HTTPException(status_code=403, detail="insufficient role")
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
            raise HTTPException(status_code=403, detail="insufficient role")
        return Principal(
            tenant_id=tenant_id or "tenant-a",
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

    @application.get("/health")
    def health(request: Request) -> dict[str, Any]:
        settings = request.app.state.settings
        return {
            "status": "ok", "environment": settings.app_env,
            "control_plane": settings.control_plane_backend,
            "session_events": settings.session_event_backend,
            "model_provider": settings.model_provider,
            "knowledge_backend": settings.knowledge_backend,
            "amazon_finance": "configured" if settings.analytics_dsn else "disabled",
            "lingxing_profit": (
                "configured"
                if _lingxing_profit_active(request.app.state.agent_registry)
                else "disabled"
            ),
            "profit_report": (
                "configured"
                if _profit_report_active(settings, request.app.state.agent_registry)
                else "disabled"
            ),
            "kingdee_cloud": (
                "configured"
                if _kingdee_cloud_active(request.app.state.agent_registry)
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
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
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
        try:
            return request.app.state.agent_runtime.run(
                payload,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                role=principal.role,
                token_budget=request.app.state.settings.run_token_budget,
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
                    on_event=emit,
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
        stored = request.app.state.session_events.list_events(
            session_id=payload.session_id, tenant_id=principal.tenant_id
        )
        if not stored:
            raise HTTPException(status_code=404, detail="session not found")
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
                    seller_id=payload.seller_id,
                    token_budget=request.app.state.settings.run_token_budget,
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
        events = request.app.state.session_events.list_events(
            session_id=session_id,
            tenant_id=principal.tenant_id,
        )
        if not events:
            raise HTTPException(status_code=404, detail="agent session not found")
        return {
            "session_id": session_id,
            "items": [event.model_dump(mode="json") for event in events],
            "count": len(events),
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
        events = request.app.state.session_events.list_events(
            session_id=session_id,
            tenant_id=principal.tenant_id,
        )
        if not events:
            raise HTTPException(status_code=404, detail="agent session not found")
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
        for task in tasks:
            child_deleted += request.app.state.session_events.delete_session(
                session_id=task.child_session_id,
                tenant_id=principal.tenant_id,
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
            },
        )
        return {
            "session_id": session_id,
            "deleted": True,
            "event_count": deleted,
            "child_event_count": child_deleted,
        }

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
        if not _amazon_finance_active(request.app.state.settings, registry):
            raise HTTPException(status_code=503, detail="ANALYTICS_DSN is not configured")
        try:
            result = request.app.state.amazon_finance_agent.run(payload)
        except AmazonFinanceQueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        request.app.state.store.audit(
            tenant_id=principal.tenant_id,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="amazon_finance.queried",
            resource_type="amazon_finance",
            resource_id=result.seller_id,
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
        if not _kingdee_cloud_active(registry):
            raise HTTPException(
                status_code=503,
                detail="Kingdee integration is not configured in agent settings",
            )
        try:
            result = request.app.state.kingdee_cloud_agent.run(payload)
        except (KingdeeQueryError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        if not _lingxing_profit_active(registry):
            raise HTTPException(
                status_code=503,
                detail="LingXing integration is not configured in agent settings",
            )
        try:
            result = request.app.state.lingxing_profit_agent.run(payload)
        except (LingXingProfitQueryError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        if not _profit_report_active(request.app.state.settings, registry):
            raise HTTPException(status_code=503, detail="ANALYTICS_DSN is not configured")
        try:
            result = request.app.state.profit_report_agent.run(payload)
        except ProfitReportQueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    @application.get("/v1/agents")
    def list_agents(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        registry: AgentRegistry = request.app.state.agent_registry
        settings = request.app.state.settings
        items = []
        for agent in registry.list():
            payload = mask_agent_integration(agent)
            payload["status"] = "active"
            if not agent.enabled:
                payload["status"] = "disabled"
            elif agent.id == "amazon-finance-query" and not _amazon_finance_active(
                settings, registry
            ):
                payload["status"] = "disabled"
            elif agent.id == "lingxing-profit-report" and not _lingxing_profit_active(registry):
                payload["status"] = "disabled"
            elif agent.id == "profit-report-query" and not _profit_report_active(
                settings, registry
            ):
                payload["status"] = "disabled"
            elif agent.id == "kingdee-cloud" and not _kingdee_cloud_active(registry):
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
        principal_from_headers(
            request, x_api_key, x_tenant_id, x_user_id, x_user_role
        )
        registry: AgentRegistry = request.app.state.agent_registry
        agent = registry.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        payload = mask_agent_integration(agent)
        if agent.id == "function-calling-runtime":
            tool_catalog = request.app.state.runtime_tool_registry.catalog()
            builtin_tools = sorted(
                item["name"] for item in tool_catalog if item.get("builtin")
            )
            optional_tools = [
                name for name in agent.allowed_tools if name not in builtin_tools
            ]
            payload["tool_catalog"] = tool_catalog
            payload["builtin_tools"] = builtin_tools
            payload["optional_tools"] = optional_tools
            payload["restrict_tools"] = bool(agent.allowed_tools)
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
        if payload.allowed_tools is not None and agent_id == "function-calling-runtime":
            catalog = {
                item["name"]: item
                for item in request.app.state.runtime_tool_registry.catalog()
            }
            unknown = sorted(set(payload.allowed_tools) - set(catalog))
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown tools: {', '.join(unknown)}",
                )
            builtin = {
                name for name, item in catalog.items() if item.get("builtin")
            }
            invalid_builtin = sorted(set(payload.allowed_tools) & builtin)
            if invalid_builtin:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "builtin tools are always enabled and cannot be configured: "
                        + ", ".join(invalid_builtin)
                    ),
                )
        try:
            updated = registry.update(agent_id, payload)
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
        items = [
            {
                "id": model.id,
                "name": model.name,
                "provider": model.provider,
                "model_name": model.model_name,
                "is_default": model.is_default,
                "supports_vision": bool(model.vision_model_name),
            }
            for model in registry.list(enabled_only=True)
        ]
        return {
            "items": items,
            "count": len(items),
            "default_model_id": registry.default_model_id(),
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
        return {
            "items": items,
            "count": len(items),
            "default_model_id": registry.default_model_id(),
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
        tool_context = ToolExecutionContext(
            session_id="catalog",
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            role=principal.role,
        )
        runtime_tools = request.app.state.runtime_tool_registry.catalog_for(tool_context)
        agent_snapshot = snapshot_agents(
            request.app.state.agent_registry,
            amazon_active=_amazon_finance_active(
                request.app.state.settings, request.app.state.agent_registry
            ),
            lingxing_active=_lingxing_profit_active(request.app.state.agent_registry),
            profit_report_active=_profit_report_active(
                request.app.state.settings, request.app.state.agent_registry
            ),
            kingdee_active=_kingdee_cloud_active(request.app.state.agent_registry),
        )
        return {
            **agent_snapshot,
            "tools": runtime_tools,
        }

    @application.get("/v1/configuration")
    def configuration(
        request: Request,
        x_api_key: str | None = Header(default=None),
        x_tenant_id: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        principal_from_headers(request, x_api_key, x_tenant_id, x_user_id, x_user_role)
        settings = request.app.state.settings
        model_registry: ModelRegistry = request.app.state.model_registry
        default_model = model_registry.default_model()
        return {
            "environment": settings.app_env,
            "persistence": {
                "control_plane": settings.control_plane_backend,
                "session_events": settings.session_event_backend,
            },
            "model": {
                "provider": default_model.provider,
                "name": default_model.model_name,
                "default_model_id": default_model.id,
                "timeout_seconds": settings.model_request_timeout_seconds,
                "max_retries": settings.model_max_retries,
                "backoff_base_seconds": settings.model_backoff_base_seconds,
            },
            "models": {
                "items": model_registry.catalog_items(),
                "count": len(model_registry.list()),
                "default_model_id": model_registry.default_model_id(),
            },
            "knowledge": {
                "backend": settings.knowledge_backend,
                "collection": settings.qdrant_collection,
                "embedding_model": settings.embedding_model,
                "top_k": settings.qdrant_top_k,
                "configured": settings.knowledge_backend == "mock" or bool(settings.qdrant_url),
            },
            "amazon_finance": {
                "configured": bool(settings.analytics_dsn),
                "data_scope": "RELEASED only",
                "statement_timeout_ms": settings.analytics_statement_timeout_ms,
            },
            "lingxing_profit": {
                "configured": _lingxing_profit_active(request.app.state.agent_registry),
                "endpoint": (
                    "/basicOpen/finance/profitReport/order/transcation/list"
                ),
                "credential_source": "agent_config",
            },
            "profit_report": {
                "configured": _profit_report_active(
                    request.app.state.settings, request.app.state.agent_registry
                ),
                "table": "lingxing_profit_order_transactions",
                "import_script": "scripts/import_lingxing_profit_xlsx.py",
            },
            "kingdee_cloud": {
                "configured": _kingdee_cloud_active(request.app.state.agent_registry),
                "method": "DynamicFormService.ExecuteBillQuery",
                "documents": [
                    "SAL_SaleOrder",
                    "SAL_OUTSTOCK",
                    "AR_receivable",
                    "AR_OtherRecAble",
                ],
                "credential_source": "agent_config",
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
            {"viewer", "admin"},
        )
        items = request.app.state.store.list_audit(
            tenant_id=principal.tenant_id, limit=limit
        )
        return {"items": items, "count": len(items)}

    frontend_dir = Path(__file__).resolve().parents[3] / "frontend"
    if frontend_dir.exists():
        application.mount("/ui", StaticFiles(directory=frontend_dir), name="ui")

        @application.get("/", include_in_schema=False)
        def console() -> FileResponse:
            return FileResponse(frontend_dir / "index.html")

    return application


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "ops_agent.api.app:app", host=settings.app_host, port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
