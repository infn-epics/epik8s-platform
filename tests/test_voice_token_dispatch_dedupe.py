import importlib.util
import os
import sys
import threading
import types
import unittest
from pathlib import Path

FILES = Path(__file__).parents[1] / "files" / "ai-platform"


def load_token_server():
    """Load voice-token-server.py (hyphenated name, needs jwt/livekit and env at import)."""
    for name, value in {
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
        "LIVEKIT_URL": "ws://livekit.invalid",
    }.items():
        os.environ.setdefault(name, value)

    sys.modules.setdefault("jwt", types.ModuleType("jwt"))
    livekit = types.ModuleType("livekit")
    livekit.api = types.ModuleType("livekit.api")
    livekit.api.ListParticipantsRequest = lambda room: types.SimpleNamespace(room=room)
    livekit.api.RoomParticipantIdentity = lambda room, identity: types.SimpleNamespace(room=room, identity=identity)
    protocol = types.ModuleType("livekit.protocol")
    dispatch = types.ModuleType("livekit.protocol.agent_dispatch")
    dispatch.CreateAgentDispatchRequest = object
    sys.modules.update({
        "livekit": livekit,
        "livekit.api": livekit.api,
        "livekit.protocol": protocol,
        "livekit.protocol.agent_dispatch": dispatch,
    })
    sys.path.insert(0, str(FILES / "voice-agent"))  # room_scope, as the ConfigMap mounts it

    spec = importlib.util.spec_from_file_location("voice_token_server", FILES / "voice-token-server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = load_token_server()


class DispatchDedupeTests(unittest.TestCase):
    def setUp(self):
        server._recent_dispatch.clear()
        server.DISPATCH_DEDUPE_SECONDS = 15.0
        self.calls = []
        self.fail_next = False

        async def fake_dispatch(room, model):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("livekit unreachable")
            self.calls.append((room, model))

        self._orig = server._dispatch_agent
        server._dispatch_agent = fake_dispatch

    def tearDown(self):
        server._dispatch_agent = self._orig

    def test_back_to_back_requests_dispatch_once(self):
        # The dashboard's connect -> disconnect -> connect race on page load.
        server.dispatch_agent("btf-argus-control-room", "minimax-m27")
        server.dispatch_agent("btf-argus-control-room", "minimax-m27")
        self.assertEqual(self.calls, [("btf-argus-control-room", "minimax-m27")])

    def test_truly_concurrent_requests_dispatch_once(self):
        # ThreadingHTTPServer serves them on different threads at the same
        # time, so the reservation has to happen before the network call.
        barrier = threading.Barrier(8)

        def hit():
            barrier.wait()
            server.dispatch_agent("btf-argus-control-room", "minimax-m27")

        threads = [threading.Thread(target=hit) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(self.calls), 1)

    def test_different_model_or_room_still_dispatches(self):
        server.dispatch_agent("btf-argus-control-room", "minimax-m27")
        server.dispatch_agent("btf-argus-control-room", "qwen36-27b")  # operator switched model
        server.dispatch_agent("sparc-argus-control-room", "minimax-m27")
        self.assertEqual(len(self.calls), 3)

    def test_rejoin_after_the_window_gets_a_fresh_agent(self):
        # The case the module docstring warns about: a later reconnect must
        # never be left without an agent.
        server.dispatch_agent("btf-argus-control-room", "minimax-m27")
        key = ("btf-argus-control-room", "minimax-m27")
        server._recent_dispatch[key] -= server.DISPATCH_DEDUPE_SECONDS + 1
        server.dispatch_agent("btf-argus-control-room", "minimax-m27")
        self.assertEqual(len(self.calls), 2)

    def test_failed_dispatch_does_not_block_the_retry(self):
        self.fail_next = True
        server.dispatch_agent("btf-argus-control-room", "minimax-m27")  # raises inside, is logged
        server.dispatch_agent("btf-argus-control-room", "minimax-m27")
        self.assertEqual(len(self.calls), 1)

    def test_window_of_zero_disables_the_guard(self):
        server.DISPATCH_DEDUPE_SECONDS = 0
        server.dispatch_agent("btf-argus-control-room", "minimax-m27")
        server.dispatch_agent("btf-argus-control-room", "minimax-m27")
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()


class EvictStaleAgentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_agents_are_evicted_humans_stay(self):
        P = lambda identity, kind: types.SimpleNamespace(identity=identity, kind=kind)
        removed = []

        class Room:
            async def list_participants(self, req):
                return types.SimpleNamespace(participants=[
                    P("operator-a", 0), P("agent-1", 4), P("operator-b", 0), P("agent-2", 4)])

            async def remove_participant(self, req):
                removed.append((req.room, req.identity))

        client = types.SimpleNamespace(room=Room())
        await server._remove_existing_agents(client, "btf-argus-control-room")
        self.assertEqual(removed, [("btf-argus-control-room", "agent-1"), ("btf-argus-control-room", "agent-2")])

    async def test_livekit_failure_does_not_raise(self):
        class Room:
            async def list_participants(self, req):
                raise RuntimeError("down")

        await server._remove_existing_agents(types.SimpleNamespace(room=Room()), "r")


class OperatorRoomTests(unittest.TestCase):
    ROOMS = {"btf-argus-control-room": "btf", "sparc-argus-control-room": "sparc"}

    def test_private_room_per_operator_is_stable_and_distinct(self):
        a = {"sub": "id-1", "preferred_username": "operator.btf"}
        b = {"sub": "id-2", "preferred_username": "operator-btf"}  # same slug, other user
        base = "btf-argus-control-room"
        self.assertEqual(server.operator_room(base, a), server.operator_room(base, a))
        self.assertNotEqual(server.operator_room(base, a), server.operator_room(base, b))
        self.assertTrue(server.operator_room(base, a).startswith(base + "--operator-btf-"))

    def test_authorize_needs_role_and_beamline(self):
        ok = lambda c, room="btf-argus-control-room": server.authorize_voice(c, room, self.ROOMS, "argus.use")[0]
        self.assertTrue(ok({"roles": ["argus.use"], "beamlines": ["btf"]}))
        self.assertTrue(ok({"roles": ["argus.use"], "beamlines": ["BTF"]}))
        self.assertFalse(ok({"roles": ["pv.read"], "beamlines": ["btf"]}))                    # viewer: no argus.use
        self.assertFalse(ok({"roles": ["argus.use"], "beamlines": ["sparc"]}))                # wrong beamline
        self.assertFalse(ok({"roles": ["argus.use"], "beamlines": []}))                       # deny by default
        self.assertFalse(ok({"roles": ["argus.use"], "beamlines": ["btf"]}, "unknown-room"))  # unmapped room
        self.assertTrue(ok({"roles": ["platform.admin"]}))
