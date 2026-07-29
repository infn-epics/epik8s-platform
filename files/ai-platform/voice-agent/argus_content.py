"""
Structured "content" extraction from argus-mcp tool results, for the
Jarvis-like chat's rich-content rendering (tables/charts/embedded control
widgets) - see events.py's send_content_* functions for the wire format
this module builds payloads for.

Built incrementally per epik8s-dashboard's Phase B plan chunking:
  B1: charts - get_history direct.
  B2: tables - list_iocs/search_pvs/list_beamline_devices.
  B3: device correlation (DeviceCatalogCache) + embedded live widgets.
  B4: automatic historical-trend chart attached to a device lookup.
Only tools actually wired into CONTENT_*_TOOLS below produce a content
event of any kind - everything else stays plain text to the LLM, same
allowlist philosophy as READ_ONLY_TOOLS/HIGHLIGHT_ARG_BY_TOOL in
argus_mcp_bridge.py. Every build_* function here is best-effort and never
raises on a shape it doesn't recognize - a resolution failure just means
"no rich content for this call", never a broken tool response (the
caller in argus_mcp_bridge.py wraps the whole dispatch in try/except
anyway, matching send_highlight's existing contract, but these functions
are written defensively in their own right since None is a normal,
expected return value here, not an error path).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from livekit import rtc

from events import send_content_chart

logger = logging.getLogger("voice-agent.argus-content")

# Tools whose result is already a get_history-shaped time series - no
# device/devgroup resolution needed, straight JSON-path extraction.
CONTENT_CHART_DIRECT_TOOLS = frozenset({"get_history"})

# Union of every tool this module knows how to extract content from -
# argus_mcp_bridge.py checks this first to skip the JSON parse entirely
# for the (majority) of tool calls with no content mapping at all.
CONTENT_TOOLS = CONTENT_CHART_DIRECT_TOOLS

# Measured live 2026-07-29: 300 points/series for a single-series chart
# serializes to ~10.8KB - comfortably fine alone, but a future 2-series
# chart (e.g. BPM X/Y, see B4) would double to ~21KB, uncomfortably close
# to typical WebRTC reliable-data-channel ceilings (commonly ~15-16KB;
# not independently re-verified against this deployment's exact LiveKit
# server version - see the live smoke test in the implementation plan).
# 200 keeps even a 2-series chart under ~14.4KB with margin.
MAX_CHART_POINTS = 200


def _downsample(samples: list[dict[str, Any]], max_points: int = MAX_CHART_POINTS) -> tuple[list[dict[str, Any]], int]:
    """Even-stride downsample that always keeps the last sample - a naive
    head-truncate would bias a wide time range toward only ever showing
    its earliest portion."""
    total = len(samples)
    if total <= max_points:
        return samples, total
    stride = total / max_points
    indices = sorted({int(i * stride) for i in range(max_points - 1)} | {total - 1})
    return [samples[i] for i in indices], total


def _iso_to_epoch_ms(iso_ts: str) -> int | None:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return None


def build_chart_content(tool_name: str, payload: dict[str, Any], *, source: str = "tool") -> dict[str, Any] | None:
    """get_history's {"history": {"pv_name", "samples": [{"timestamp",
    "value", "severity"}, ...]}} -> one chart content dict (kwargs for
    events.send_content_chart) with a single series. None if the shape
    doesn't match (e.g. an empty/error result) - see module docstring."""
    history = payload.get("history")
    if not isinstance(history, dict):
        return None
    pv_name = history.get("pv_name")
    samples = history.get("samples")
    if not pv_name or not isinstance(samples, list) or not samples:
        return None

    kept, total = _downsample(samples)
    t: list[int] = []
    v: list[float] = []
    for sample in kept:
        ts = _iso_to_epoch_ms(sample.get("timestamp", ""))
        value = sample.get("value")
        if ts is None or not isinstance(value, (int, float)):
            continue
        t.append(ts)
        v.append(float(value))
    if not t:
        return None

    content: dict[str, Any] = {
        "tool": tool_name,
        "title": pv_name,
        "series": [{"label": pv_name, "pv": pv_name, "t": t, "v": v}],
        "source": source,
    }
    if len(t) < total:
        content["truncated"] = {"points": total - len(t)}
    return content


async def emit_content_for_tool(room: rtc.Room, tool_name: str, result_text: str) -> None:
    """Dispatch point called from argus_mcp_bridge.py's _call(), right
    after the existing highlight fire-and-forget block, with the exact
    same JSON text already extracted from the MCP tool result (no second
    round-trip to the tool). Deliberately allowed to raise - the caller
    wraps this in the same try/except Exception: logger.exception(...)
    pattern already used for send_highlight, so a failure here is logged
    but never breaks the actual tool response the LLM is waiting on.

    Cheap fast-path: skip the JSON parse entirely for the (large)
    majority of tool calls that have no content mapping at all.
    """
    if tool_name not in CONTENT_TOOLS:
        return

    payload = json.loads(result_text)
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return

    if tool_name in CONTENT_CHART_DIRECT_TOOLS:
        content = build_chart_content(tool_name, payload)
        if content is not None:
            await send_content_chart(room, **content)
