"""
Operator-confirmed gate for the state-changing ARGUS tools.

The LLM can only *propose* a write. Nothing runs until the signed-in operator
confirms it in the dashboard, and the gate enforces, server-side:

  * capability   - the session was dispatched with can_act (Keycloak role
                   argus.actions, decided by the token service, never by the
                   client or the model);
  * identity     - only the operator this room belongs to can confirm; a
                   confirm_action from any other participant is ignored;
  * exactness    - the call that runs is the one stored when the request was
                   made (the operator saw its label), never arguments taken from
                   the confirm message;
  * scope        - restart_ioc is limited to the beamline's own namespace;
  * one at a time, expiring, and rate limited;
  * audit        - every proposal, confirmation, refusal and result is logged
                   with the operator's identity.

Pure asyncio, no LiveKit imports, so it is unit-testable.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import deque
from typing import Any, Awaitable, Callable

logger = logging.getLogger("voice-agent.action-gate")

WRITE_TOOLS = frozenset({
    "set_pv", "set_pv_value", "restart_ioc", "execute_procedure", "create_logbook_entry",
})


def namespace_from_url(url: str) -> str | None:
    """'http://svc.<ns>.svc.cluster.local:8000/sse' -> '<ns>'."""
    m = re.search(r"//[^/:]+?\.([a-z0-9-]+)\.svc(?:\.cluster\.local)?(?::\d+)?/", url + "/")
    return m.group(1) if m else None


def _clip(value: Any, n: int = 120) -> str:
    s = str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_action(tool: str, args: dict[str, Any]) -> str:
    """The exact wording the operator confirms (Italian, like the UI)."""
    if tool in ("set_pv", "set_pv_value"):
        value = args.get("pv_value", args.get("value"))
        return f"Imposta {_clip(args.get('pv_name'))} = {_clip(value)}"
    if tool == "restart_ioc":
        return f"Riavvia l'IOC {_clip(args.get('pod_name'))} (namespace {_clip(args.get('namespace'))})"
    if tool == "execute_procedure":
        params = args.get("params")
        return f"Esegui la procedura {_clip(args.get('name'))}" + (f" con parametri {_clip(params)}" if params else "")
    if tool == "create_logbook_entry":
        return f"Scrivi nel logbook: «{_clip(args.get('title'))}»"
    return f"{tool} {_clip(args)}"


class ActionGate:
    def __init__(
        self,
        operator_identity: str,
        can_act: bool,
        publish_request: Callable[[str, str, str | None, int], Awaitable[None]],
        beamline_namespace: str | None = None,
        timeout_s: float = 60.0,
        max_per_minute: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.operator_identity = operator_identity
        self.can_act = can_act
        self._publish = publish_request
        self.beamline_namespace = beamline_namespace
        self.timeout_s = timeout_s
        self.max_per_minute = max_per_minute
        self._clock = clock
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._recent: deque[float] = deque()

    # ---- called by the tool wrapper ------------------------------------
    async def execute(self, tool: str, args: dict[str, Any], run: Callable[[dict[str, Any]], Awaitable[str]]) -> str:
        who = self.operator_identity
        if not self.can_act:
            logger.warning("REFUSED %s for %s: no argus.actions role", tool, who)
            return "Azione non consentita: il tuo account non ha il permesso di eseguire azioni tramite ARGUS."
        args = dict(args)
        if tool == "restart_ioc" and self.beamline_namespace:
            if args.get("namespace") not in (None, "", self.beamline_namespace):
                logger.warning("REFUSED restart_ioc for %s: namespace %r != %r", who, args.get("namespace"), self.beamline_namespace)
                return f"Azione rifiutata: posso riavviare solo IOC del namespace {self.beamline_namespace}."
            args["namespace"] = self.beamline_namespace
        if self._pending:
            return "C'è già un'azione in attesa di conferma: attendi la risposta dell'operatore."
        now = self._clock()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= self.max_per_minute:
            logger.warning("RATE-LIMITED %s for %s", tool, who)
            return "Troppe azioni in poco tempo: riprova tra un minuto."
        self._recent.append(now)

        action_id = uuid.uuid4().hex
        label = describe_action(tool, args)
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[action_id] = fut
        logger.info("PROPOSED %s [%s] by %s: %s", tool, action_id, who, label)
        try:
            await self._publish(action_id, label, None, int(self.timeout_s * 1000))
            try:
                confirmed = await asyncio.wait_for(fut, self.timeout_s)
            except asyncio.TimeoutError:
                logger.info("EXPIRED %s [%s] for %s", tool, action_id, who)
                return "Conferma scaduta: l'azione non è stata eseguita."
        finally:
            self._pending.pop(action_id, None)
        if not confirmed:
            logger.info("CANCELLED %s [%s] by %s", tool, action_id, who)
            return "L'operatore ha annullato l'azione: non è stata eseguita."
        logger.info("CONFIRMED %s [%s] by %s: running", tool, action_id, who)
        try:
            result = await run(args)
        except Exception:
            logger.exception("FAILED %s [%s] for %s", tool, action_id, who)
            raise
        logger.info("DONE %s [%s] for %s", tool, action_id, who)
        return result

    # ---- called by the data-channel handler ----------------------------
    def on_confirm(self, action_id: Any, confirmed: Any, sender_identity: str | None) -> bool:
        """Resolve a pending action. Returns True if it was accepted."""
        if sender_identity != self.operator_identity:
            logger.warning("IGNORED confirm_action from %r (room belongs to %r)", sender_identity, self.operator_identity)
            return False
        fut = self._pending.get(action_id) if isinstance(action_id, str) else None
        if fut is None or fut.done():
            return False
        fut.set_result(confirmed is True)
        return True
