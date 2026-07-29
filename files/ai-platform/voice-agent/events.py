"""
Data-channel event senders matching the JSON schema documented in the
epik8s-dashboard frontend (src/voice/events.js, src/voice/README.md). Keep
these two in sync - this module IS the server side of that wire contract.

`phase` and `transcript`'s `metrics` field (both added for the Jarvis-like
animation/debug-latency-panel feature) carry a documented quirk: `llm`
phase events may fire more than one start/end pair for a single turn_id
when the model performs a tool call mid-turn (confirmed live against
livekit-agents 1.6.7 - see agent.py's ArgusAgent.llm_node). Frontend
reducers must treat this as normal, not an error - see
src/voice/events.js's reducePhaseEvent.

`content` (kind: table/chart/widget - built up incrementally across the
"Phase B" rich-content feature, see argus_content.py) is the rich-content
channel: tables, historical trend charts, and embedded live control
widgets, extracted server-side from otherwise-plain-text tool results.
Deliberately no turn_id - ordered into the frontend's chat feed purely by
`ts`, same as highlight.
"""
from __future__ import annotations

import json
import time
from typing import Any

from livekit import rtc

EVENT_HIGHLIGHT = "highlight"
EVENT_TRANSCRIPT = "transcript"
EVENT_CONFIRM_REQUEST = "confirm_request"
EVENT_CONFIRM_ACTION = "confirm_action"
EVENT_PHASE = "phase"
EVENT_CONTENT = "content"

DEFAULT_HIGHLIGHT_TTL_MS = 8000


async def _publish(room: rtc.Room, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    await room.local_participant.publish_data(data, reliable=True)


async def send_highlight(
    room: rtc.Room,
    device_id: str,
    reason: str,
    ttl_ms: int = DEFAULT_HIGHLIGHT_TTL_MS,
) -> None:
    """Highlight a device widget in the dashboard for ttl_ms milliseconds.

    device_id is matched against a widget's pvPrefix/deviceId by the
    frontend's matchDeviceId() (src/voice/events.js) - exact, prefix, and
    common-EPICS-suffix-stripped matches all work, so passing a bare PV
    name here is fine even if it doesn't exactly equal the widget's
    configured pvPrefix.
    """
    await _publish(room, {
        "type": EVENT_HIGHLIGHT,
        "device_id": device_id,
        "reason": reason,
        "ttl_ms": ttl_ms,
    })


async def send_transcript(
    room: rtc.Room,
    role: str,
    text: str,
    final: bool,
    metrics: dict[str, float] | None = None,
) -> None:
    """role must be 'user' or 'assistant', matching isTranscriptEvent().

    metrics, when given, is a flat {field_name: milliseconds} dict (see
    agent.py's _on_conversation_item for the exact fields per role) -
    omitted entirely from the payload when None/empty, not sent as
    null/{}, so existing frontend fixtures/tests that don't expect this
    key stay valid.
    """
    payload: dict[str, Any] = {
        "type": EVENT_TRANSCRIPT,
        "role": role,
        "text": text,
        "final": final,
        "ts": int(time.time() * 1000),
    }
    if metrics:
        payload["metrics"] = metrics
    await _publish(room, payload)


async def send_phase(room: rtc.Room, turn_id: str, phase: str, edge: str) -> None:
    """Real-time, fine-grained phase-transition events driving the
    frontend's Jarvis-like animation and per-phase latency display.

    phase: 'stt' | 'llm' | 'tts'. edge: 'start' | 'end'. Emitted from
    ArgusAgent's stt_node/llm_node/tts_node overrides in agent.py.

    CAVEAT: 'llm' may emit more than one start/end pair for one turn_id if
    the model performs a tool call mid-turn (confirmed live via
    livekit-agents 1.6.7's agent_activity.py max_tool_steps loop) - a
    second 'start' while already open is a no-op for duration purposes,
    only the *last* 'end' closes the phase (see the frontend's
    reducePhaseEvent for the exact semantics).

    CAVEAT: 'stt' 'start' fires when the user's mic audio track ends
    (button released), not when stt_node was first invoked - the latter
    spans the entire mic-hold window and would be a misleading signal for
    an animation meant to show "the system is now processing your
    speech".
    """
    await _publish(room, {
        "type": EVENT_PHASE,
        "turn_id": turn_id,
        "phase": phase,
        "edge": edge,
        "ts": int(time.time() * 1000),
    })


async def send_content_chart(
    room: rtc.Room,
    tool: str,
    title: str,
    series: list[dict[str, Any]],
    unit: str | None = None,
    source: str = "tool",
    truncated: dict[str, int] | None = None,
) -> None:
    """A time-series chart, built from a get_history-shaped tool result (or,
    in a later phase, an automatic historical-trend enrichment attached to
    a device lookup - see source: "tool" vs "auto_enrichment").

    Each series is {"label", "pv", "t": [epoch_ms, ...], "v": [float, ...]}
    - columnar, not an array of {t,v} objects, to roughly halve the wire
    size (LiveKit reliable data-channel messages have a practical size
    ceiling - keep chart/table payloads capped, see argus_content.py's
    MAX_CHART_POINTS). The frontend converts to {x,y} points at its own
    component boundary (src/components/consoles/voiceContentUI.jsx).

    No turn_id, deliberately, matching send_highlight's own precedent -
    the frontend orders content blocks into the chat feed purely by `ts`.
    """
    payload: dict[str, Any] = {
        "type": EVENT_CONTENT,
        "kind": "chart",
        "tool": tool,
        "title": title,
        "series": series,
        "source": source,
        "ts": int(time.time() * 1000),
    }
    if unit:
        payload["unit"] = unit
    if truncated:
        payload["truncated"] = truncated
    await _publish(room, payload)


async def send_confirm_request(
    room: rtc.Room,
    action_id: str,
    label: str,
    device_id: str | None = None,
    timeout_ms: int = 15000,
) -> None:
    """Not called anywhere in this read-only-tools phase - kept here so the
    wire schema lives in one place. The next phase that adds write tools
    MUST call this before executing any write, and MUST wait for the
    matching confirm_action (role='user') before proceeding - see the
    platform README's "Voice Assistant" safety section. The frontend's
    banner never executes anything itself, only forwards the operator's
    choice; the server-side wait/gate has to live here.
    """
    payload: dict[str, Any] = {
        "type": EVENT_CONFIRM_REQUEST,
        "action_id": action_id,
        "label": label,
        "timeout_ms": timeout_ms,
    }
    if device_id:
        payload["device_id"] = device_id
    await _publish(room, payload)
