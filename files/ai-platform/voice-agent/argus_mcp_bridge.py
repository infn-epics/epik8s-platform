"""
Explicitly-allowlisted bridge from per-beamline argus-mcp servers (mixed
read/write tool surface) into livekit-agents FunctionTool objects.

Deliberately NOT using livekit-agents' native `mcp.MCPServerHTTP` for these
servers: that integration exposes a server's ENTIRE tool surface with no
per-tool filtering, and argus-mcp mixes safe read tools with `set_pv`/
`set_pv_value` (writes any PV to any value via EPICS caput, no
confirmation, no allowlist) and `restart_ioc` (deletes a live IOC pod to
force a restart). See the platform README's "Voice Assistant" section for
the full write-tool safety writeup. kubernetes-mcp/rag-mcp, by contrast,
ARE wired via the native integration directly in agent.py, since their
entire tool surface is read-only by construction (RBAC grants only
get/list/watch; rag-mcp only ever reads Qdrant) - no filtering needed
there.

READ_ONLY_TOOLS is an ALLOWLIST, not a denylist: any tool a future
argus-mcp-server release adds (including one that doesn't exist yet) is
excluded by default unless deliberately added here. Tool names are from
argus-mcp-server's argus/mcp_server/tools/*.py (registry.py's ToolRegistry,
name= kwarg on each ToolDefinition) - re-verify this list against that
repo if it has moved on since this was written.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client

from livekit import rtc
from livekit.agents import function_tool
from livekit.agents.llm import FunctionTool

from argus_content import DeviceCatalogCache, emit_content_for_tool
from events import send_highlight

logger = logging.getLogger("voice-agent.argus-mcp-bridge")

READ_ONLY_TOOLS = frozenset({
    "get_pv", "get_pv_info", "get_pv_value",
    "search_pvs", "list_iocs", "machine_summary",
    "get_history", "get_alarm_history",
    "list_beamline_devices", "get_device", "device_status",
    "diagnose_device", "beamline_status",
    "search_documentation", "search_knowledge_base", "get_config_history",
    "get_logs", "search_pod_logs",
    "search_snapshots", "get_snapshot",
})

# Deliberately excluded (write/control, or disarmed-but-wired-so-exclude-
# pre-emptively): set_pv, set_pv_value, restart_ioc, execute_procedure,
# create_logbook_entry.

# Tools whose arguments name a device/PV worth highlighting on the
# dashboard when the LLM calls them - {tool_name: argument_key}.
HIGHLIGHT_ARG_BY_TOOL = {
    "get_pv": "pv_name",
    "get_pv_info": "pv_name",
    "get_pv_value": "pv_name",
    "get_device": "device_name",
    "device_status": "device_name",
    "diagnose_device": "device_name",
}


@dataclass
class ArgusMcpServerConfig:
    name: str  # short id, e.g. "btf-argus" - used for logging and descriptions
    url: str   # e.g. http://argus-argus-helm-chart-argus-mcp.<ns>.svc.cluster.local:8000/sse
    title: str = ""


class ArgusMcpBridge:
    """Owns one SSE MCP session for one argus-mcp server and exposes its
    allowlisted tools as livekit-agents FunctionTools."""

    def __init__(self, room: rtc.Room, server: ArgusMcpServerConfig):
        self._room = room
        self._server = server
        self._session: ClientSession | None = None
        self._streams_cm = None
        self._session_cm = None
        # Lambda, not a direct reference, since self._session is None at
        # __init__ time and only set by connect() - the cache reads it
        # lazily on its first lookup() call, well after connect().
        self._catalog = DeviceCatalogCache(lambda: self._session)

    async def connect(self) -> None:
        self._streams_cm = sse_client(self._server.url)
        read, write = await self._streams_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        logger.info("connected to argus-mcp %s (%s)", self._server.name, self._server.url)

    async def close(self) -> None:
        if self._session_cm is not None:
            await self._session_cm.__aexit__(None, None, None)
        if self._streams_cm is not None:
            await self._streams_cm.__aexit__(None, None, None)

    async def discover_tools(self, excluded_names: frozenset[str] = frozenset()) -> list[FunctionTool]:
        """List the server's tools and return only the allowlisted ones,
        wrapped as callable FunctionTools. Logs (does not raise on) every
        tool it skips, so a deploy that adds a new server-side tool is
        visible in the agent's logs rather than silently either exposed or
        silently missing. ``excluded_names`` prevents collisions with tools
        supplied by the session's central MCP servers."""
        assert self._session is not None, "call connect() first"
        listing = await self._session.list_tools()
        tools: list[FunctionTool] = []
        for tool in listing.tools:
            if tool.name not in READ_ONLY_TOOLS:
                logger.info(
                    "argus-mcp %s: NOT exposing non-allowlisted tool %r to the LLM",
                    self._server.name, tool.name,
                )
                continue
            if tool.name in excluded_names:
                logger.info(
                    "argus-mcp %s: central MCP provides tool %r; skipping duplicate",
                    self._server.name,
                    tool.name,
                )
                continue
            tools.append(self._wrap_tool(tool.name, tool.description or "", tool.inputSchema or {"type": "object", "properties": {}}))
        logger.info("argus-mcp %s: exposing %d/%d tools", self._server.name, len(tools), len(listing.tools))
        return tools

    def _wrap_tool(self, name: str, description: str, input_schema: dict[str, Any]) -> FunctionTool:
        # entrypoint() selects exactly one Argus MCP server for the current
        # LiveKit room before this bridge is created. Keep the server's native
        # tool name: model providers are substantially more reliable with
        # familiar names such as ``list_iocs`` than with a synthetic
        # ``btf-argus__list_iocs`` prefix. There is no cross-beamline collision
        # because tools from other beamlines never enter this AgentSession.
        exposed_name = name
        qualified_description = f"[{self._server.title or self._server.name}] {description}"

        async def _call(raw_arguments: dict[str, Any]) -> str:
            assert self._session is not None
            result = await self._session.call_tool(name, raw_arguments)
            text = "\n".join(c.text for c in result.content if getattr(c, "text", None))

            highlight_arg = HIGHLIGHT_ARG_BY_TOOL.get(name)
            device_name = str(raw_arguments[highlight_arg]) if highlight_arg and raw_arguments.get(highlight_arg) else None

            if device_name:
                try:
                    await send_highlight(
                        self._room,
                        device_id=device_name,
                        reason=f"ARGUS ({self._server.title or self._server.name}) · {name}",
                    )
                except Exception:
                    # A highlight is a UI nicety - never let it break the
                    # actual tool call/response the LLM is waiting on.
                    logger.exception("failed to publish highlight event for %s", exposed_name)

            try:
                # device_name/catalog are no-ops for every tool except
                # CONTENT_WIDGET_TOOLS (get_device/device_status/
                # diagnose_device) - harmless to pass unconditionally since
                # emit_content_for_tool only reads them there. Reuses the
                # exact same device_name already extracted above for the
                # highlight event rather than re-deriving it.
                await emit_content_for_tool(self._room, name, text, device_name=device_name, catalog=self._catalog)
            except Exception:
                # Same rationale as the highlight try/except above: rich
                # content (tables/charts/widgets) is a UI enrichment, never
                # allowed to break the actual tool response.
                logger.exception("failed to emit content event for %s", exposed_name)

            return text or "(empty result)"

        # NOTE (version-sensitive): this targets livekit-agents' dynamic/
        # raw-schema function_tool usage - the same primitive its own
        # built-in MCP integration uses internally to bridge MCP tool
        # schemas. Verify `function_tool(..., raw_schema=...)` against the
        # actually-installed livekit-agents version before relying on this
        # in production; the signature has moved across releases.
        #
        # When raw_schema is given, function_tool's own `name=`/`description=`
        # kwargs are ignored entirely (confirmed against 1.6.7's tool_context.py:
        # deco_raw() only reads raw_schema["name"]/["parameters"], never the
        # outer args) - name/description/parameters must all live inside the
        # raw_schema dict itself, per RawFunctionDescription.
        return function_tool(
            raw_schema={
                "name": exposed_name,
                "description": qualified_description,
                "parameters": input_schema,
            },
        )(_call)
