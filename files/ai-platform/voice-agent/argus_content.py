"""
Structured "content" extraction from argus-mcp tool results, for the
Jarvis-like chat's rich-content rendering (tables/charts/embedded control
widgets) - see events.py's send_content_* functions for the wire format
this module builds payloads for.

Built incrementally per epik8s-dashboard's Phase B plan chunking:
  B1: charts - get_history direct.
  B2: tables - list_iocs/search_pvs/list_beamline_devices.
  B3: device correlation (DeviceCatalogCache) + embedded live widgets -
      get_device/device_status/diagnose_device.
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

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from livekit import rtc
from mcp import ClientSession

from events import send_content_chart, send_content_table, send_content_widget

logger = logging.getLogger("voice-agent.argus-content")

# Tools whose result is already a get_history-shaped time series - no
# device/devgroup resolution needed, straight JSON-path extraction.
CONTENT_CHART_DIRECT_TOOLS = frozenset({"get_history"})

# Tools whose result is a row-listing best rendered as a table.
CONTENT_TABLE_TOOLS = frozenset({"list_iocs", "search_pvs", "list_beamline_devices"})

# Tools whose result is a single-device lookup, best rendered as an
# embedded live widget once correlated against the beamline device catalog
# (see DeviceCatalogCache below) - same tool set argus_mcp_bridge.py's
# HIGHLIGHT_ARG_BY_TOOL already keys off of, all using the "device_name"
# argument.
CONTENT_WIDGET_TOOLS = frozenset({"get_device", "device_status", "diagnose_device"})

# Union of every tool this module knows how to extract content from -
# argus_mcp_bridge.py checks this first to skip the JSON parse entirely
# for the (majority) of tool calls with no content mapping at all.
CONTENT_TOOLS = CONTENT_CHART_DIRECT_TOOLS | CONTENT_TABLE_TOOLS | CONTENT_WIDGET_TOOLS

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

# devgroup -> key_pvs role to auto-chart on a device lookup (B4). Populated
# only for the three groups where list_beamline_devices' key_pvs is
# actually non-empty in this catalog (confirmed live 2026-08-03: mag/vac/
# cool devices have it, mot/cam/etc. don't) - motors get their own .RBV
# suffix fallback below instead (resolve_chart_pv), everything else gets no
# auto-chart at all rather than a guessed PV.
DEVGROUP_CHART_ROLE = {
    "mag": "current_readback",
    "vac": "pressure_readback",
    "cool": "temp_readback",
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


# How long a DeviceCatalogCache snapshot is trusted before refreshing.
# Beamline device catalogs (deploy/values.yaml, read via git) change on
# deploys, not live operation, so a few minutes of staleness is harmless -
# this just avoids one extra MCP round-trip per device lookup.
CATALOG_TTL_S = 300


class DeviceCatalogCache:
    """Per-ArgusMcpBridge, TTL-cached snapshot of list_beamline_devices(),
    keyed by case-insensitive device name.

    Exists to correlate get_device/device_status/diagnose_device's
    ChannelFinder-backed response (confirmed live 2026-08-03: no devgroup,
    no iocprefix, no key_pvs - just an echo of the input name as
    primary_pv) against the YAML-backed devgroup/iocprefix a widget needs.
    It also doubles as the only real existence check available: those three
    tools report `"status": "success"` even for a device name that does
    not exist at all, so a catalog miss here is what actually means "no
    such device" - not the tool's own status field.

    Exact match only, deliberately - a fuzzy/substring match risks
    embedding the WRONG device's live widget, which is worse than
    embedding none.
    """

    def __init__(self, get_session):
        self._get_session = get_session
        self._by_name: dict[str, dict[str, Any]] = {}
        self._loaded_at: float = 0.0

    @property
    def session(self):
        """The bridge's live MCP session - exposed so B4's auto-chart
        enrichment can issue its own get_history calls without needing a
        second get_session callable threaded through separately."""
        return self._get_session()

    async def _refresh(self) -> None:
        session = self._get_session()
        assert session is not None, "call after ArgusMcpBridge.connect()"
        result = await session.call_tool("list_beamline_devices", {})
        text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
        payload = json.loads(text)
        devices = payload.get("devices")
        if not isinstance(devices, list):
            return
        self._by_name = {
            d["name"].lower(): d for d in devices if isinstance(d, dict) and d.get("name")
        }
        self._loaded_at = time.monotonic()

    async def lookup(self, name: str) -> dict[str, Any] | None:
        if not name:
            return None
        stale = (time.monotonic() - self._loaded_at) > CATALOG_TTL_S
        if stale or not self._by_name:
            try:
                await self._refresh()
            except Exception:
                logger.exception("failed to refresh device catalog")
        return self._by_name.get(name.strip().lower())


def build_widget_content(tool_name: str, beamline_device: dict[str, Any]) -> dict[str, Any] | None:
    """A resolved BeamlineDevice catalog entry -> one widget content dict
    (kwargs for events.send_content_widget). None when the devgroup isn't
    one this beamline's widget registry actually renders (see
    DEVGROUP_WIDGET_MAP's coverage note) - not every device is embeddable,
    and guessing a wrong widget_type is worse than showing none."""
    name = beamline_device.get("name")
    iocprefix = beamline_device.get("iocprefix")
    if not name or not iocprefix:
        return None
    devgroup = beamline_device.get("devgroup")
    widget_type = DEVGROUP_WIDGET_MAP.get(devgroup)
    if not widget_type:
        return None
    pv_prefix = resolve_device_pv_prefix(name, iocprefix)
    return {
        "tool": tool_name,
        "title": f"{name} ({devgroup})",
        "device_id": pv_prefix,
        "pv_prefix": pv_prefix,
        "widget_type": widget_type,
        "config": {"pvPrefix": pv_prefix, "viewMode": "essential"},
    }


def resolve_chart_pv(beamline_device: dict[str, Any]) -> list[dict[str, str]] | None:
    """A resolved BeamlineDevice -> the PV(s) worth auto-charting on a
    device lookup, as a list of {"label", "pv"} (normally one entry, two
    for the BPM X/Y heuristic). None when this devgroup has no known
    auto-chart PV - v1 coverage is deliberately narrow (mag/vac/cool via
    key_pvs, confirmed live 2026-08-04 to actually be populated for those
    three; mot via the .RBV suffix fallback since real motor devices'
    key_pvs is confirmed empty; plus the BPM name heuristic) - guessing
    wrong here is worse than showing no chart at all."""
    name = beamline_device.get("name")
    iocprefix = beamline_device.get("iocprefix")
    if not name or not iocprefix:
        return None
    pv_prefix = resolve_device_pv_prefix(name, iocprefix)

    if "bpm" in name.lower():
        return [
            {"label": "X", "pv": f"{pv_prefix}:SA:X"},
            {"label": "Y", "pv": f"{pv_prefix}:SA:Y"},
        ]

    devgroup = beamline_device.get("devgroup")
    chart_role = DEVGROUP_CHART_ROLE.get(devgroup)
    key_pvs = beamline_device.get("key_pvs") or {}
    if chart_role and key_pvs.get(chart_role):
        pv = key_pvs[chart_role]
        return [{"label": pv, "pv": pv}]

    if devgroup == "mot":
        pv = f"{pv_prefix}.RBV"
        return [{"label": pv, "pv": pv}]

    return None


# Auto-chart enrichment issues its own get_history call(s) *inside* the
# tool response the LLM is waiting on (see argus_mcp_bridge.py's _call() -
# emit_content_for_tool is awaited before returning). A hard per-PV cap
# keeps a slow archiver from stalling the whole turn for long - this same
# tool family (diagnose_device/device_status) has already been observed
# LIVE to hang 3-10s on its own internal providers (see DeviceCatalogCache
# docstring / the B3 live smoke test), so this is not theoretical caution.
AUTO_CHART_TIMEOUT_S = 4.0
AUTO_CHART_HOURS = 1.0


async def _fetch_auto_chart_series(session: ClientSession, pv_name: str, label: str) -> tuple[dict[str, Any] | None, int]:
    """One PV's recent history -> a chart series dict {label, pv, t, v},
    plus how many points the downsample dropped. Best-effort: any failure
    (timeout, bad JSON, empty samples) returns (None, 0), never raises -
    one PV's history being unavailable shouldn't cancel a BPM's other
    series, let alone the widget that already got sent alongside it."""
    try:
        result = await asyncio.wait_for(
            session.call_tool("get_history", {"pv_name": pv_name, "hours": AUTO_CHART_HOURS}),
            timeout=AUTO_CHART_TIMEOUT_S,
        )
    except Exception:
        return None, 0
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, 0
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None, 0
    history = payload.get("history")
    if not isinstance(history, dict):
        return None, 0
    samples = history.get("samples")
    if not isinstance(samples, list) or not samples:
        return None, 0
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
        return None, 0
    return {"label": label, "pv": pv_name, "t": t, "v": v}, max(0, total - len(t))


async def build_auto_chart_content(session: ClientSession, tool_name: str, beamline_device: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch and shape a trend chart for whatever PV(s) resolve_chart_pv
    finds for this device - kwargs for events.send_content_chart with
    source="auto_enrichment" (vs. "tool" for a direct get_history call in
    B1's CONTENT_CHART_DIRECT_TOOLS path). The (possibly two, for BPM)
    get_history calls run concurrently; None if every one comes back
    empty/errored/timed out - see _fetch_auto_chart_series."""
    pv_specs = resolve_chart_pv(beamline_device)
    if not pv_specs:
        return None
    results = await asyncio.gather(
        *(_fetch_auto_chart_series(session, spec["pv"], spec["label"]) for spec in pv_specs)
    )
    series = [s for s, _ in results if s is not None]
    if not series:
        return None
    truncated_points = sum(n for _, n in results)
    content: dict[str, Any] = {
        "tool": tool_name,
        "title": f"{beamline_device.get('name')} - trend",
        "series": series,
        "source": "auto_enrichment",
    }
    if truncated_points:
        content["truncated"] = {"points": truncated_points}
    return content


async def emit_content_for_tool(
    room: rtc.Room,
    tool_name: str,
    result_text: str,
    *,
    device_name: str | None = None,
    catalog: "DeviceCatalogCache | None" = None,
) -> None:
    """Dispatch point called from argus_mcp_bridge.py's _call(), right
    after the existing highlight fire-and-forget block, with the exact
    same JSON text already extracted from the MCP tool result (no second
    round-trip to the tool). Deliberately allowed to raise - the caller
    wraps this in the same try/except Exception: logger.exception(...)
    pattern already used for send_highlight, so a failure here is logged
    but never breaks the actual tool response the LLM is waiting on.

    Cheap fast-path: skip the JSON parse entirely for the (large)
    majority of tool calls that have no content mapping at all.

    device_name/catalog are only used for CONTENT_WIDGET_TOOLS - the
    caller passes the same device_name it already extracted for the
    highlight event (HIGHLIGHT_ARG_BY_TOOL happens to key all three widget
    tools off "device_name" too) and its per-server DeviceCatalogCache.
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
    elif tool_name in CONTENT_WIDGET_TOOLS:
        if not device_name or catalog is None:
            return
        beamline_device = await catalog.lookup(device_name)
        if beamline_device is None:
            # Not in the deployed catalog - see DeviceCatalogCache's
            # docstring for why this (not payload["status"]) is the real
            # existence check for these three tools.
            return
        content = build_widget_content(tool_name, beamline_device)
        if content is not None:
            await send_content_widget(room, **content)

        # B4: transparently attach a trend chart alongside the widget, for
        # whatever PV(s) resolve_chart_pv finds - independent of whether
        # the widget itself was sent (a devgroup with no widget mapping,
        # e.g. diag, might still resolve nothing here either, that's fine).
        if catalog.session is not None:
            chart_content = await build_auto_chart_content(catalog.session, tool_name, beamline_device)
            if chart_content is not None:
                await send_content_chart(room, **chart_content)
