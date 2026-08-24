import sys
import unittest
from pathlib import Path


MODULE_DIR = Path(__file__).parents[1] / "files" / "ai-platform" / "voice-agent"
sys.path.insert(0, str(MODULE_DIR))

from room_scope import RoomScopeError, require_allowed_room, select_server_for_room


SERVERS = [
    {"name": "sparc-argus", "roomName": "sparc-argus-control-room"},
    {"name": "euaps-argus", "roomName": "euaps-argus-control-room"},
]


class RoomScopeTests(unittest.TestCase):
    def test_selects_only_exact_room_match(self):
        selected = select_server_for_room("sparc-argus-control-room", SERVERS)
        self.assertEqual(selected["name"], "sparc-argus")

    def test_rejects_unknown_room(self):
        with self.assertRaises(RoomScopeError):
            select_server_for_room("btf-argus-control-room", SERVERS)

    def test_rejects_ambiguous_room(self):
        duplicate = [*SERVERS, {"name": "other", "roomName": "sparc-argus-control-room"}]
        with self.assertRaises(RoomScopeError):
            select_server_for_room("sparc-argus-control-room", duplicate)

    def test_token_allowlist_uses_exact_match(self):
        self.assertEqual(
            require_allowed_room("sparc-argus-control-room", ["sparc-argus-control-room"]),
            "sparc-argus-control-room",
        )
        with self.assertRaises(RoomScopeError):
            require_allowed_room("sparc", ["sparc-argus-control-room"])


if __name__ == "__main__":
    unittest.main()
