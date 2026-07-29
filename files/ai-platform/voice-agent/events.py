"""
Data-channel event senders matching the JSON schema documented in the
epik8s-dashboard frontend (src/voice/events.js, src/voice/README.md). Keep
these two in sync - this module IS the server side of that wire contract.
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


async def send_transcript(room: rtc.Room, role: str, text: str, final: bool) -> None:
    """role must be 'user' or 'assistant', matching isTranscriptEvent()."""
    await _publish(room, {
        "type": EVENT_TRANSCRIPT,
        "role": role,
        "text": text,
        "final": final,
        "ts": int(time.time() * 1000),
    })


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
