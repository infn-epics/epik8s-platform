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

from events import send_content_chart, send_content_table

logger = logging.getLogger("voice-agent.argus-content")

# Tools whose result is already a get_history-shaped time series - no
# device/devgroup resolution needed, straight JSON-path extraction.
CONTENT_CHART_DIRECT_TOOLS = frozenset({"get_history"})

# Tools whose result is a row-listing best rendered as a table.
CONTENT_TABLE_TOOLS = frozenset({"list_iocs", "search_pvs", "list_beamline_devices"})

# Union of every tool this module knows how to extract content from -
# argus_mcp_bridge.py checks this first to skip the JSON parse entirely
# for the (majority) of tool calls with no content mapping at all.
CONTENT_TOOLS = CONTENT_CHART_DIRECT_TOOLS | CONTENT_TABLE_TOOLS

MAX_TABLE_ROWS = 50

# devgroup -> widget_type. list_beamline_devices' own rows already carry
# devgroup directly (confirmed live 2026-08-03 against the real sparc-argus
# catalog), so table rows can resolve a clickable widget hint without B3's
# DeviceCatalogCache (that cache is only needed to correlate get_device/
# diagnose_device's *different*, devgroup-less response shape). Coverage is
# intentionally narrow - v1 only maps groups this beamline's own widget
# registry (src/widgets/registry.js) actually renders; any other devgroup
# (diag, rf, timing, io, modulator, or missing) still gets device_id/
# pv_prefix but no widget_type, which the frontend treats as "not
# embeddable" rather than guessing.
DEVGROUP_WIDGET_MAP = {
    "mag": "power-supply",
    "vac": "vacuum",
    "cool": "cooling",
    "mot": "motor",
    "cam": "camera",
}


def resolve_device_pv_prefix(name: str, iocprefix: str) -> str:
    """f"{iocprefix}:{name}" for the vast majority of devices - except BPMs,
    which have no devgroup of their own in this catalog (confirmed live -
    no `bpm` devgroup exists; BPM PVs only surface indirectly via unrelated
    IOCs' alias lists) and instead use a name heuristic matching this
    deployment's own deploy/values.yaml convention (e.g. literal
    'AC1BPM01:SA:X' - the bare device name, no iocprefix)."""
    if "bpm" in name.lower():
        return name
    return f"{iocprefix}:{name}"

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


def _table_row_from_device(device: dict[str, Any]) -> dict[str, Any] | None:
    name = device.get("name")
    iocprefix = device.get("iocprefix")
    if not name or not iocprefix:
        return None
    devgroup = device.get("devgroup")
    pv_prefix = resolve_device_pv_prefix(name, iocprefix)
    row: dict[str, Any] = {
        "cells": {
            "name": name,
            "devgroup": devgroup or "",
            "devfunc": device.get("devfunc") or "",
            "ioc_name": device.get("ioc_name") or "",
        },
        "device_id": pv_prefix,
        "pv_prefix": pv_prefix,
    }
    widget_type = DEVGROUP_WIDGET_MAP.get(devgroup)
    if widget_type:
        row["widget_type"] = widget_type
    return row


def _build_beamline_devices_table(payload: dict[str, Any]) -> dict[str, Any] | None:
    devices = payload.get("devices")
    if not isinstance(devices, list) or not devices:
        return None
    rows = [r for r in (_table_row_from_device(d) for d in devices) if r is not None]
    if not rows:
        return None
    total = len(rows)
    kept = rows[:MAX_TABLE_ROWS]
    devgroup = payload.get("devgroup")
    title = f"Devices ({len(kept)} of {total}" + (f", devgroup={devgroup})" if devgroup else ")")
    content: dict[str, Any] = {
        "tool": "list_beamline_devices",
        "title": title,
        "columns": ["name", "devgroup", "devfunc", "ioc_name"],
        "rows": kept,
    }
    if total > len(kept):
        content["truncated"] = {"rows": total - len(kept)}
    return content


def _build_iocs_table(payload: dict[str, Any]) -> dict[str, Any] | None:
    iocs = payload.get("iocs")
    if not isinstance(iocs, list) or not iocs:
        return None
    total = len(iocs)
    kept = iocs[:MAX_TABLE_ROWS]
    rows = [
        {
            "cells": {
                "name": ioc.get("name", ""),
                "namespace": ioc.get("namespace", ""),
                "phase": ioc.get("phase", ""),
                "ready": ioc.get("ready", ""),
                "restart_count": ioc.get("restart_count", ""),
            },
        }
        for ioc in kept
    ]
    content: dict[str, Any] = {
        "tool": "list_iocs",
        "title": f"IOCs ({len(rows)} of {total})",
        "columns": ["name", "namespace", "phase", "ready", "restart_count"],
        "rows": rows,
    }
    if total > len(rows):
        content["truncated"] = {"rows": total - len(rows)}
    return content


def _build_search_pvs_table(payload: dict[str, Any]) -> dict[str, Any] | None:
    channels = payload.get("channels")
    if not isinstance(channels, list) or not channels:
        return None
    total = len(channels)
    rows: list[dict[str, Any]] = []
    for ch in channels[:MAX_TABLE_ROWS]:
        name = ch.get("name")
        if not name:
            continue
        props = ch.get("properties") or {}
        rows.append({
            "cells": {
                "name": name,
                "devgroup": props.get("devgroup", ""),
                "ioc": props.get("iocName", ""),
            },
            # A raw ChannelFinder PV, not necessarily a whole device -
            # tagged as device_id only (highlight-able) rather than
            # pv_prefix/widget_type, which imply an embeddable device.
            "device_id": name,
        })
    if not rows:
        return None
    content: dict[str, Any] = {
        "tool": "search_pvs",
        "title": f"PVs ({len(rows)} of {total})",
        "columns": ["name", "devgroup", "ioc"],
        "rows": rows,
    }
    if total > len(rows):
        content["truncated"] = {"rows": total - len(rows)}
    return content


def build_table_content(tool_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch to the per-tool row-builder above. None (no content event)
    for an empty/unrecognized result shape - see module docstring."""
    if tool_name == "list_beamline_devices":
        return _build_beamline_devices_table(payload)
    if tool_name == "list_iocs":
        return _build_iocs_table(payload)
    if tool_name == "search_pvs":
        return _build_search_pvs_table(payload)
    return None


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
    elif tool_name in CONTENT_TABLE_TOOLS:
        content = build_table_content(tool_name, payload)
        if content is not None:
            await send_content_table(room, **content)
