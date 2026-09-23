import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "files" / "ai-platform" / "voice-agent"))

from action_gate import ActionGate, describe_action, namespace_from_url  # noqa: E402

OP = "operator-ann-1a2b3c"


def make_gate(can_act=True, **kw):
    sent = []

    async def publish(action_id, label, device_id, timeout_ms):
        sent.append((action_id, label, timeout_ms))

    gate = ActionGate(OP, can_act, publish, beamline_namespace="btf", **kw)
    return gate, sent


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def _propose(self, gate, tool="set_pv", args=None):
        ran = []

        async def run(a):
            ran.append(a)
            return "ok"

        task = asyncio.create_task(gate.execute(tool, args or {"pv_name": "BTF:MAG:I", "pv_value": 3}, run))
        return task, ran

    async def test_runs_only_after_operator_confirms_and_uses_stored_args(self):
        gate, sent = make_gate()
        task, ran = await self._propose(gate)
        await asyncio.sleep(0)
        self.assertEqual(ran, [])                                  # nothing runs before confirmation
        self.assertIn("BTF:MAG:I = 3", sent[0][1])                 # operator sees the exact PV and value
        self.assertTrue(gate.on_confirm(sent[0][0], True, OP))
        self.assertEqual(await task, "ok")
        self.assertEqual(ran, [{"pv_name": "BTF:MAG:I", "pv_value": 3}])

    async def test_cancel_and_timeout_do_not_run(self):
        gate, sent = make_gate(timeout_s=0.05)
        task, ran = await self._propose(gate)
        await asyncio.sleep(0)
        gate.on_confirm(sent[0][0], False, OP)
        self.assertIn("annullato", await task)
        task, ran2 = await self._propose(gate)
        self.assertIn("scaduta", await task)
        self.assertEqual(ran + ran2, [])

    async def test_confirm_from_another_participant_or_unknown_id_is_ignored(self):
        gate, sent = make_gate(timeout_s=0.1)
        task, ran = await self._propose(gate)
        await asyncio.sleep(0)
        self.assertFalse(gate.on_confirm(sent[0][0], True, "operator-mallory-ffffff"))
        self.assertFalse(gate.on_confirm("not-an-id", True, OP))
        self.assertFalse(gate.on_confirm(None, True, None))
        self.assertIn("scaduta", await task)
        self.assertEqual(ran, [])

    async def test_only_a_literal_true_confirms(self):
        gate, sent = make_gate()
        task, ran = await self._propose(gate)
        await asyncio.sleep(0)
        gate.on_confirm(sent[0][0], "yes", OP)                     # truthy but not True => cancel
        self.assertIn("annullato", await task)
        self.assertEqual(ran, [])

    async def test_no_capability_refuses_without_asking(self):
        gate, sent = make_gate(can_act=False)
        task, ran = await self._propose(gate)
        self.assertIn("non consentita", await task)
        self.assertEqual((sent, ran), ([], []))

    async def test_restart_ioc_is_limited_to_the_beamline_namespace(self):
        gate, sent = make_gate()
        task, ran = await self._propose(gate, "restart_ioc", {"pod_name": "ioc-1", "namespace": "sparc"})
        self.assertIn("solo IOC del namespace btf", await task)
        self.assertEqual((sent, ran), ([], []))
        task, ran = await self._propose(gate, "restart_ioc", {"pod_name": "ioc-1"})
        await asyncio.sleep(0)
        gate.on_confirm(sent[0][0], True, OP)
        await task
        self.assertEqual(ran[0]["namespace"], "btf")

    async def test_one_pending_at_a_time_and_rate_limit(self):
        gate, sent = make_gate(max_per_minute=2)
        t1, _ = await self._propose(gate)
        await asyncio.sleep(0)
        t2, _ = await self._propose(gate)
        self.assertIn("già un'azione", await t2)
        gate.on_confirm(sent[0][0], False, OP)
        await t1
        t3, _ = await self._propose(gate)
        await asyncio.sleep(0)
        gate.on_confirm(sent[1][0], False, OP)
        await t3
        t4, _ = await self._propose(gate)
        self.assertIn("Troppe azioni", await t4)


class HelperTests(unittest.TestCase):
    def test_namespace_from_url(self):
        self.assertEqual(namespace_from_url("http://argus-x.btf.svc.cluster.local:8000/sse"), "btf")
        self.assertIsNone(namespace_from_url("http://localhost:8000/sse"))

    def test_labels(self):
        self.assertIn("procedura park", describe_action("execute_procedure", {"name": "park"}))
        self.assertIn("logbook", describe_action("create_logbook_entry", {"title": "T"}))
