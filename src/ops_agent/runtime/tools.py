from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ValidationError

from .domain import ToolCall, ToolResult, ToolRisk
from .tracing import span
from .connectors import ToolBindingRegistry
from ..connections import ConnectionRegistry


@dataclass(frozen=True)
class ToolExecutionContext:
    session_id: str
    tenant_id: str
    user_id: str
    role: str = "admin"
    approved_call_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_tool_names: frozenset[str] | None = None
    delegation_depth: int = 0
    agent_id: str = "function-calling-runtime"
    model_id: str | None = None
    connection_ids: tuple[str, ...] = ()
    resource_scope: dict[str, tuple[str, ...]] = field(default_factory=dict)
    connection_scope_enforced: bool = False
    deadline: float | None = None
    cancellation_event: Any = None
    explicit_memory_consent: bool = False
    explicit_memory_forget: bool = False
    memory_snapshot: tuple[dict[str, Any], ...] = ()


ToolHandler = Callable[[BaseModel, ToolExecutionContext], Any]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: ToolHandler
    risk: ToolRisk = "low"
    requires_approval: bool = False
    timeout_seconds: float = 10.0
    concurrency_safe: bool = True
    source: str = "local"
    builtin: bool = False
    allowed_roles: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"viewer", "operator", "approver", "admin"}
        )
    )
    allowed_tenants: frozenset[str] | None = None
    parameters_schema: dict[str, Any] | None = None

    def visible_to(self, context: ToolExecutionContext) -> bool:
        return (
            context.role in self.allowed_roles
            and (
                context.allowed_tool_names is None
                or self.name in context.allowed_tool_names
            )
            and (
                self.allowed_tenants is None
                or context.tenant_id in self.allowed_tenants
            )
        )

    def model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema
                or self.arguments_model.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"tool already registered: {definition.name}")
        self._tools[definition.name] = definition

    def get(
        self, name: str, context: ToolExecutionContext | None = None
    ) -> ToolDefinition:
        try:
            definition = self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc
        if context is not None and not definition.visible_to(context):
            raise PermissionError(f"tool is not visible to this principal: {name}")
        return definition

    def schemas(
        self, context: ToolExecutionContext | None = None
    ) -> list[dict[str, Any]]:
        definitions = self._tools.values()
        if context is not None:
            definitions = (
                item for item in definitions if item.visible_to(context)
            )
        return [definition.model_schema() for definition in definitions]

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.name,
                "name": item.name,
                "description": item.description,
                "risk": item.risk,
                "requires_approval": item.requires_approval,
                "approval": item.requires_approval,
                "timeout_seconds": item.timeout_seconds,
                "mode": item.source,
                "source": item.source,
                "builtin": item.builtin,
                "allowed_roles": sorted(item.allowed_roles),
                "allowed_tenants": (
                    sorted(item.allowed_tenants)
                    if item.allowed_tenants is not None
                    else None
                ),
            }
            for item in self._tools.values()
        ]

    def catalog_for(
        self, context: ToolExecutionContext | None = None
    ) -> list[dict[str, Any]]:
        items = self._tools.values()
        if context is not None:
            items = (item for item in items if item.visible_to(context))
        return [
            {
                "id": item.name,
                "name": item.name,
                "description": item.description,
                "risk": item.risk,
                "requires_approval": item.requires_approval,
                "approval": item.requires_approval,
                "timeout_seconds": item.timeout_seconds,
                "mode": item.source,
                "source": item.source,
                "builtin": item.builtin,
            }
            for item in items
        ]

    def builtin_names(self) -> frozenset[str]:
        return frozenset(
            item.name for item in self._tools.values() if item.builtin
        )

    def tool_names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def resolve_allowed_tools(self, optional_tools: list[str] | None) -> set[str] | None:
        if optional_tools is None or not optional_tools:
            return None
        return set(self.builtin_names()) | set(optional_tools)


class ToolGuard(Protocol):
    def check(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: ToolExecutionContext,
    ) -> None: ...


class ApprovalGuard:
    def check(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: ToolExecutionContext,
    ) -> None:
        if definition.requires_approval and call.call_id not in context.approved_call_ids:
            raise PermissionError(f"tool requires approval: {definition.name}")


class ConnectorAccessGuard:
    def __init__(
        self,
        bindings: ToolBindingRegistry,
        connections: ConnectionRegistry,
    ) -> None:
        self.bindings = bindings
        self.connections = connections

    def check(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: ToolExecutionContext,
    ) -> None:
        binding = self.bindings.find(definition.name)
        if binding is None:
            return
        connection = self.bindings.resolve_connection(
            context.tenant_id, definition.name, self.connections
        )
        if (
            context.connection_scope_enforced
            and connection.id not in context.connection_ids
        ):
            raise PermissionError(
                f"connector is outside delegated scope: {binding.connector_type}"
            )


class ToolExecutor:
    """Deterministic validation and execution pipeline for every tool source."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        guards: list[ToolGuard] | None = None,
    ) -> None:
        self.registry = registry
        self.guards = list(guards or [ApprovalGuard()])

    def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult:
        with span("tool.execute", tool_name=call.name, session_id=context.session_id):
            return self._execute(call, context)

    def _execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult:
        started = time.perf_counter()
        try:
            definition = self.registry.get(call.name, context)
            arguments = definition.arguments_model.model_validate(call.arguments)
            for guard in self.guards:
                guard.check(definition, call, context)

            if context.cancellation_event is not None and context.cancellation_event.is_set():
                raise TimeoutError(f"tool cancelled before execution: {call.name}")
            timeout = definition.timeout_seconds
            if context.deadline is not None:
                timeout = min(timeout, max(0.0, context.deadline - time.monotonic()))
            if timeout <= 0:
                raise TimeoutError(f"tool deadline exceeded before execution: {call.name}")
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{call.name}")
            future = pool.submit(definition.handler, arguments, context)
            try:
                output = future.result(timeout=timeout)
            except FutureTimeout as exc:
                if context.cancellation_event is not None:
                    context.cancellation_event.set()
                future.cancel()
                raise TimeoutError(
                    f"tool timed out after {timeout:g}s: {call.name}"
                ) from exc
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            if isinstance(output, BaseModel):
                output = output.model_dump(mode="json")
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.name,
                ok=True,
                output=output,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        except (KeyError, ValidationError, PermissionError, TimeoutError) as exc:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.name,
                ok=False,
                error=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
