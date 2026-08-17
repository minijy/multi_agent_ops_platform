from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, ConfigDict, Field

from .tools import ToolDefinition, ToolExecutionContext, ToolRegistry


class MCPServerConfig(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    transport: Literal["stdio", "streamable-http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: Path | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30, ge=1, le=300)
    enabled: bool = True
    fail_on_startup_error: bool = False
    allowed_roles: set[str] = Field(
        default_factory=lambda: {"viewer", "operator", "approver", "admin"}
    )
    allowed_tenants: set[str] | None = None


class MCPConfig(BaseModel):
    servers: list[MCPServerConfig] = Field(default_factory=list)


class MCPArguments(BaseModel):
    model_config = ConfigDict(extra="allow")


def _public_name(server: str, raw_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", raw_name)
    candidate = f"mcp__{server}__{normalized}"
    if len(candidate) <= 64:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
    return f"{candidate[:51]}_{digest}"


class MCPClientManager:
    """Keeps configured MCP sessions on a dedicated asyncio loop."""

    def __init__(self, config_path: Path, registry: ToolRegistry) -> None:
        self.config_path = config_path
        self.registry = registry
        self.configs: dict[str, MCPServerConfig] = {}
        self.statuses: dict[str, dict[str, Any]] = {}
        self._queues: dict[str, asyncio.Queue[Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _load_config(self) -> list[MCPServerConfig]:
        if not self.config_path.is_file():
            return []
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        config = MCPConfig.model_validate(payload)
        names = [server.name for server in config.servers]
        if len(names) != len(set(names)):
            raise ValueError("duplicate MCP server name")
        return [server for server in config.servers if server.enabled]

    def start(self) -> None:
        servers = self._load_config()
        if not servers:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mcp-client-loop", daemon=True
        )
        self._thread.start()
        for config in servers:
            self.configs[config.name] = config
            try:
                tools = self._submit(
                    self._start_server(config), timeout=config.timeout_seconds + 5
                )
                self._register_tools(config, tools)
                self.statuses[config.name] = {
                    "state": "ready",
                    "tool_count": len(tools),
                    "error": None,
                }
            except Exception as exc:
                self.statuses[config.name] = {
                    "state": "error",
                    "tool_count": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                if config.fail_on_startup_error:
                    self.stop()
                    raise

    def _submit(self, coroutine: Any, *, timeout: float) -> Any:
        if self._loop is None:
            raise RuntimeError("MCP manager is not running")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout)

    @staticmethod
    def _stdio_environment(config: MCPServerConfig) -> dict[str, str]:
        safe_names = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
        return {
            **{name: os.environ[name] for name in safe_names if name in os.environ},
            **config.env,
        }

    async def _start_server(self, config: MCPServerConfig) -> list[Any]:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[list[Any]] = loop.create_future()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._queues[config.name] = queue
        self._tasks[config.name] = asyncio.create_task(
            self._serve(config, queue, ready),
            name=f"mcp-{config.name}",
        )
        return await ready

    async def _serve(
        self,
        config: MCPServerConfig,
        queue: asyncio.Queue[Any],
        ready: asyncio.Future[list[Any]],
    ) -> None:
        try:
            async with AsyncExitStack() as stack:
                if config.transport == "stdio":
                    if not config.command:
                        raise ValueError(
                            f"MCP stdio server {config.name} needs command"
                        )
                    streams = await stack.enter_async_context(
                        stdio_client(
                            StdioServerParameters(
                                command=config.command,
                                args=config.args,
                                env=self._stdio_environment(config),
                                cwd=config.cwd,
                            )
                        )
                    )
                    read_stream, write_stream = streams
                else:
                    if not config.url:
                        raise ValueError(
                            f"MCP HTTP server {config.name} needs url"
                        )
                    streams = await stack.enter_async_context(
                        streamablehttp_client(
                            config.url,
                            headers=config.headers,
                            timeout=config.timeout_seconds,
                        )
                    )
                    read_stream, write_stream, _session_id = streams
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(
                            seconds=config.timeout_seconds
                        ),
                    )
                )
                await session.initialize()
                result = await session.list_tools()
                tools = list(result.tools)
                cursor = result.nextCursor
                while cursor:
                    result = await session.list_tools(cursor=cursor)
                    tools.extend(result.tools)
                    cursor = result.nextCursor
                ready.set_result(tools)

                while True:
                    job = await queue.get()
                    if job is None:
                        break
                    raw_name, arguments, future = job
                    try:
                        result = await session.call_tool(
                            raw_name,
                            arguments,
                            read_timeout_seconds=timedelta(
                                seconds=config.timeout_seconds
                            ),
                        )
                        future.set_result(self._normalize_result(result))
                    except Exception as exc:
                        future.set_exception(exc)
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            while not queue.empty():
                job = queue.get_nowait()
                if job is not None and not job[2].done():
                    job[2].set_exception(exc)

    def _register_tools(self, config: MCPServerConfig, tools: list[Any]) -> None:
        raw_names: set[str] = set()
        for item in tools:
            if item.name in raw_names:
                raise ValueError(f"duplicate MCP tool from {config.name}: {item.name}")
            raw_names.add(item.name)
            public_name = _public_name(config.name, item.name)
            raw_name = item.name

            def execute(
                arguments: MCPArguments,
                _context: ToolExecutionContext,
                *,
                server_name: str = config.name,
                tool_name: str = raw_name,
            ) -> Any:
                return self.call(
                    server_name,
                    tool_name,
                    arguments.model_dump(exclude_none=True),
                )

            self.registry.register(
                ToolDefinition(
                    name=public_name,
                    description=item.description or f"MCP tool {raw_name}",
                    arguments_model=MCPArguments,
                    parameters_schema=item.inputSchema,
                    handler=execute,
                    timeout_seconds=config.timeout_seconds + 1,
                    source=f"mcp:{config.name}",
                    allowed_roles=frozenset(config.allowed_roles),
                    allowed_tenants=(
                        frozenset(config.allowed_tenants)
                        if config.allowed_tenants is not None
                        else None
                    ),
                )
            )

    @staticmethod
    def _normalize_result(result: Any) -> Any:
        blocks: list[dict[str, Any]] = []
        for item in result.content:
            kind = getattr(item, "type", "unknown")
            if kind == "text":
                blocks.append({"type": "text", "text": item.text})
            else:
                blocks.append(
                    {"type": kind, "content": "[non-text MCP content omitted]"}
                )
        output: dict[str, Any] = {"content": blocks}
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            output["structured_content"] = structured
        if result.isError:
            raise RuntimeError(json.dumps(output, ensure_ascii=False, default=str))
        return output

    async def _call(
        self, server_name: str, raw_name: str, arguments: dict[str, Any]
    ) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queues[server_name].put((raw_name, arguments, future))
        return await future

    def call(
        self, server_name: str, raw_name: str, arguments: dict[str, Any]
    ) -> Any:
        config = self.configs[server_name]
        return self._submit(
            self._call(server_name, raw_name, arguments),
            timeout=config.timeout_seconds + 1,
        )

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"name": name, **status}
            for name, status in sorted(self.statuses.items())
        ]

    async def _close_all(self) -> None:
        for queue in self._queues.values():
            await queue.put(None)
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._queues.clear()
        self._tasks.clear()

    def stop(self) -> None:
        if self._loop is None:
            return
        try:
            self._submit(self._close_all(), timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._loop.close()
            self._loop = None
            self._thread = None
