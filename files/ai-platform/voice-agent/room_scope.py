"""Pure helpers for binding a LiveKit room to one beamline MCP server."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class RoomScopeError(ValueError):
    """Raised when a room cannot be mapped to exactly one beamline."""


def select_server_for_room(
    room_name: str,
    servers: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Return the single MCP server explicitly assigned to ``room_name``.

    Exact matching is deliberate: deriving a beamline from a room prefix makes
    naming conventions part of the authorization boundary and can silently
    broaden access when names overlap.
    """
    room_name = str(room_name or "").strip()
    if not room_name:
        raise RoomScopeError("LiveKit room name is empty")

    matches = [server for server in servers if server.get("roomName") == room_name]
    if len(matches) != 1:
        raise RoomScopeError(
            f"room {room_name!r} must map to exactly one ARGUS MCP server; "
            f"found {len(matches)}"
        )
    return matches[0]


def require_allowed_room(room_name: str, allowed_rooms: Iterable[str]) -> str:
    """Validate a token request against the configured exact room allowlist."""
    room_name = str(room_name or "").strip()
    allowed = {str(room).strip() for room in allowed_rooms if str(room).strip()}
    if room_name not in allowed:
        raise RoomScopeError(f"room {room_name!r} is not configured")
    return room_name
