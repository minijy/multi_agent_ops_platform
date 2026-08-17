from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from ..config import Settings
from ..agent_registry import AgentRegistry, create_agent_registry
from ..model_registry import ModelRegistry, create_model_registry
from .agent_loop import AgentRuntime
from .amazon_finance_tool import register_amazon_finance_tool
from .kingdee_cloud_tool import register_kingdee_cloud_tool
from .lingxing_profit_tool import register_lingxing_profit_tool
from .profit_report_tool import register_profit_report_tool
from .attachments import LocalAttachmentStore
from .governance import RuntimeGovernanceStore, create_runtime_governance_store
from .mcp_client import MCPClientManager
from .model_router import create_model_router_from_registry
from .observability import create_metrics_store
from .sandbox import SandboxRunner, register_sandbox_tools
from .session_events import create_session_event_store
from .skills import SkillRegistry, register_skill_tool
from .subagents import SubagentManager, register_subagent_tool
from .tools import ToolExecutor, ToolRegistry
from .tracing import configure_tracing


@dataclass
class RuntimeStack:
    settings: Settings
    agent_registry: AgentRegistry
    model_registry: ModelRegistry
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    mcp_manager: MCPClientManager
    session_events: object
    metrics_store: object
    governance_store: RuntimeGovernanceStore
    attachment_store: LocalAttachmentStore
    sandbox_runner: SandboxRunner
    agent_runtime: AgentRuntime
    subagent_manager: SubagentManager


@contextmanager
def open_runtime_stack(settings: Settings) -> Iterator[RuntimeStack]:
    """Build the shared Agent Runtime stack used by API and external workers."""
    configure_tracing(settings)
    agent_registry = create_agent_registry(settings.agent_definitions_path)
    model_registry = create_model_registry(settings.model_definitions_path, settings)
    tool_registry = ToolRegistry()
    register_amazon_finance_tool(tool_registry, settings)
    register_lingxing_profit_tool(tool_registry, agent_registry, timeout_seconds=30.0)
    register_kingdee_cloud_tool(tool_registry, agent_registry, timeout_seconds=45.0)
    register_profit_report_tool(tool_registry, settings)
    skill_registry = SkillRegistry.from_paths(settings.skills_paths)
    register_skill_tool(tool_registry, skill_registry)
    sandbox_runner = SandboxRunner(
        settings.sandbox_workspace_root,
        timeout_seconds=settings.sandbox_timeout_seconds,
        max_output_bytes=settings.sandbox_max_output_bytes,
    )
    register_sandbox_tools(tool_registry, sandbox_runner, settings)
    mcp_manager = MCPClientManager(settings.mcp_config_path, tool_registry)
    mcp_manager.start()
    session_events = create_session_event_store(settings)
    metrics_store = create_metrics_store(settings)
    governance_store = create_runtime_governance_store(settings)
    attachment_store = LocalAttachmentStore(
        settings.attachment_path,
        max_image_bytes=settings.attachment_max_image_bytes,
        max_image_pixels=settings.attachment_max_image_pixels,
    )
    agent_runtime = AgentRuntime(
        router=create_model_router_from_registry(model_registry, settings),
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        event_store=session_events,
        attachment_store=attachment_store,
        skill_registry=skill_registry,
        governance_store=governance_store,
        metrics_store=metrics_store,
        max_tool_steps=settings.max_tool_steps,
        max_attachments_per_message=settings.attachment_max_images_per_message,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
    )
    subagent_manager = SubagentManager(
        runtime=agent_runtime,
        registry=tool_registry,
        event_store=session_events,
        governance_store=governance_store,
        settings=settings,
    )
    register_subagent_tool(tool_registry, subagent_manager)
    stack = RuntimeStack(
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        mcp_manager=mcp_manager,
        session_events=session_events,
        metrics_store=metrics_store,
        governance_store=governance_store,
        attachment_store=attachment_store,
        sandbox_runner=sandbox_runner,
        agent_runtime=agent_runtime,
        subagent_manager=subagent_manager,
    )
    try:
        yield stack
    finally:
        subagent_manager.shutdown()
        mcp_manager.stop()
