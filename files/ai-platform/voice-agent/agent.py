"""
ARGUS voice agent (experimental, READ-ONLY) - a livekit-agents worker
providing a Jarvis-like voice interface to the accelerator control system.

Wires together:
  - STT: argus-stt   (OpenAI-compatible faster-whisper, aiPlatform.argusStt)
  - LLM: litellm gateway (OpenAI-compatible, aiPlatform.litellm)
  - TTS: argus-tts   (OpenAI-compatible Piper, aiPlatform.argusTts)
  - Tools:
      * kubernetes-mcp + rag-mcp via livekit-agents' native MCP
        integration - their ENTIRE tool surface is exposed as-is, since
        both are read-only by construction (see mcp.yaml's RBAC / Qdrant
        read-only client).
      * one ArgusMcpBridge per configured beamline argus-mcp server
        (argus_mcp_bridge.py) - an EXPLICIT read-only tool allowlist,
        since those servers mix read tools with set_pv/set_pv_value/
        restart_ioc/execute_procedure/create_logbook_entry.

THIS PHASE IS READ-ONLY BY DESIGN. No write/control tool is exposed to the
LLM. A future write-capable phase must, at minimum:
  1. gate every write tool call behind events.send_confirm_request() and
     wait for the matching confirm_action from the dashboard's
     Conferma/Annulla banner before calling the underlying MCP tool
     (see events.py's send_confirm_request docstring), AND
  2. add a server-side confirmation/allowlist/rate-limit gate to
     argus-mcp-server itself, which has none today - a voice agent must
     not be the only thing standing between "the LLM decided to call
     set_pv" and "a real PV changes state", especially since STT
     mis-transcription (a wrong PV name, a wrong number) is a real failure
     mode voice interfaces have that text chat mostly doesn't.

API SURFACE NOTE (read before deploying): this targets livekit-agents'
unified Agent/AgentSession API (~0.12+) and its built-in `mcp` module. The
exact names/signatures below - `mcp.MCPServerHTTP`, `Agent(tools=,
mcp_servers=)`, `AgentSession.start(...)`, the `conversation_item_added`
event and its payload shape - have moved across livekit-agents releases.
Verify each against the pinned version actually installed (see the `pip
install` line in templates/ai-platform/voice-agent.yaml) with a real `pip
show livekit-agents` + a quick smoke test before relying on this in
production; this was written without the ability to execute-test it
against a live install.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import AsyncIterable

import httpx
import openai as openai_sdk
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, mcp
from livekit.plugins import openai, silero

from argus_mcp_bridge import ArgusMcpBridge, ArgusMcpServerConfig
from events import send_transcript

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice-agent")

STT_BASE_URL = os.environ["STT_BASE_URL"]
STT_MODEL = os.environ.get("STT_MODEL", "Systran/faster-whisper-base")
TTS_BASE_URL = os.environ["TTS_BASE_URL"]
TTS_MODEL = os.environ.get("TTS_MODEL", "speaches-ai/piper-it_IT-riccardo-x_low")
TTS_VOICE = os.environ.get("TTS_VOICE", "it_IT-riccardo-x_low")
LLM_BASE_URL = os.environ["LLM_BASE_URL"]
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3-8b")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "none")
# Only the LLM call needs a proxy - it's the one endpoint that (for the
# k8sda site) is an external HTTPS gateway rather than an in-cluster
# Service. Deliberately NOT read from HTTP_PROXY/HTTPS_PROXY: those are
# unset before this process starts (see __main__ below) specifically so
# livekit-agents' own worker-registration and per-job Room.connect() calls
# (both in-cluster, both LiveKit-internal proxy handling that ignores
# NO_PROXY) never see them. This is a separate, explicit opt-in so the LLM
# client can go through the proxy without reopening that bug.
LLM_HTTP_PROXY = os.environ.get("LLM_HTTP_PROXY", "")

# Central, read-only-by-construction MCP servers (empty string = disabled).
KUBERNETES_MCP_URL = os.environ.get("KUBERNETES_MCP_URL", "")
RAG_MCP_URL = os.environ.get("RAG_MCP_URL", "")

# JSON list of {"name","url","title"} - one entry per beamline argus-mcp
# server, mirrors aiPlatform.librechat.argusMcpServers in values.yaml
# (same servers, different consumer). Example:
#   [{"name":"btf-argus","url":"http://argus-argus-helm-chart-argus-mcp.btf.svc.cluster.local:8000/sse","title":"BTF ARGUS"}]
ARGUS_MCP_SERVERS = json.loads(os.environ.get("ARGUS_MCP_SERVERS_JSON", "[]"))

SYSTEM_PROMPT = """Sei ARGUS, l'assistente vocale del sistema di controllo dell'acceleratore.
Rispondi in italiano, in modo breve e chiaro, adatto all'ascolto in sala controllo.
Puoi consultare stato macchina, valori di PV, storico allarmi, IOC, dispositivi,
log e documentazione tramite gli strumenti disponibili.
Le tue risposte vengono lette ad alta voce: non elencare MAI un risultato di uno
strumento parola per parola (es. una lista di decine o centinaia di dispositivi/PV).
Se uno strumento restituisce molti elementi, riassumi solo i conteggi aggregati e le
2-3 categorie più rilevanti in una o due frasi, poi chiedi all'operatore se vuole
dettagli su una categoria specifica. Non usare mai elenchi puntati, tabelle,
markdown o simboli come "---": tutto il testo diventa audio parlato. Ogni risposta
deve stare in poche frasi, come una vera risposta a voce fra colleghi in sala
controllo, mai un report scritto.
NON hai la capacità di scrivere su alcuna PV né di riavviare alcun IOC in questa fase:
se ti viene chiesto di farlo, spiega chiaramente che questa funzione non è ancora
abilitata su questo canale vocale e che l'azione va eseguita tramite l'interfaccia
di controllo standard (dashboard o console Phoebus).
Quando consulti un dispositivo o una PV specifica tramite uno strumento, il fatto che
quello strumento evidenzi il widget corrispondente sulla dashboard è un effetto
collaterale intenzionale - non serve menzionarlo all'operatore.
"""


# Matches lines that are pure markdown noise (horizontal rules, bullet/
# heading markers) with nothing else worth speaking - the LLM is told not
# to produce these (see SYSTEM_PROMPT), but this is a defensive second
# layer: confirmed live, a text chunk that's just "---" (or similar) makes
# argus-tts/piper synthesize genuinely zero audio frames for that chunk,
# which livekit-agents' TTS stream adapter then surfaces as a hard
# APIError that kills the ENTIRE reply - not just that one chunk - even
# though every other chunk synthesized fine.
_MARKDOWN_NOISE_LINE = re.compile(r"^\s*([-=*#_~]{2,}|[-*•]\s*)\s*$")


class ArgusAgent(Agent):
    async def tts_node(self, text, model_settings):
        async def _sanitized() -> AsyncIterable[str]:
            async for chunk in text:
                lines = [ln for ln in chunk.splitlines() if not _MARKDOWN_NOISE_LINE.match(ln)]
                cleaned = "\n".join(lines)
                if cleaned.strip():
                    yield cleaned

        return Agent.default.tts_node(self, _sanitized(), model_settings)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    native_mcp_servers = []
    if KUBERNETES_MCP_URL:
        native_mcp_servers.append(mcp.MCPServerHTTP(url=KUBERNETES_MCP_URL))
    if RAG_MCP_URL:
        native_mcp_servers.append(mcp.MCPServerHTTP(url=RAG_MCP_URL))

    bridges: list[ArgusMcpBridge] = []
    argus_tools = []
    for entry in ARGUS_MCP_SERVERS:
        cfg = ArgusMcpServerConfig(name=entry["name"], url=entry["url"], title=entry.get("title", entry["name"]))
        bridge = ArgusMcpBridge(ctx.room, cfg)
        try:
            await bridge.connect()
        except Exception:
            logger.exception("could not connect to argus-mcp server %s (%s) - skipping it for this session", cfg.name, cfg.url)
            continue
        bridges.append(bridge)
        argus_tools.extend(await bridge.discover_tools())

    async def _cleanup() -> None:
        for bridge in bridges:
            await bridge.close()

    ctx.add_shutdown_callback(_cleanup)

    agent = ArgusAgent(instructions=SYSTEM_PROMPT, tools=argus_tools, mcp_servers=native_mcp_servers)

    llm_http_client = httpx.AsyncClient(proxy=LLM_HTTP_PROXY) if LLM_HTTP_PROXY else None
    llm_client = openai_sdk.AsyncClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, http_client=llm_http_client)

    session = AgentSession(
        stt=openai.STT(base_url=STT_BASE_URL, api_key="none", model=STT_MODEL),
        llm=openai.LLM(model=LLM_MODEL, client=llm_client),
        tts=openai.TTS(base_url=TTS_BASE_URL, api_key="none", model=TTS_MODEL, voice=TTS_VOICE),
        vad=silero.VAD.load(),
    )

    @session.on("conversation_item_added")
    def _on_conversation_item(event) -> None:
        # Mirror every completed turn onto the data channel as a `final:
        # true` transcript event, matching TranscriptPanel's expectations
        # in epik8s-dashboard (partial/streaming transcript is left for a
        # follow-up - see events.py).
        item = event.item
        role = "assistant" if getattr(item, "role", None) == "assistant" else "user"
        text = getattr(item, "text_content", None) or ""
        if not text:
            return
        asyncio.create_task(send_transcript(ctx.room, role, text, final=True))

    await session.start(agent=agent, room=ctx.room)


if __name__ == "__main__":
    # http_proxy="" is deliberate, not an oversight: livekit-agents' own
    # WorkerOptions defaults http_proxy from HTTPS_PROXY/HTTP_PROXY env vars
    # (worker.py: `if not is_given(http_proxy): http_proxy =
    # os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")`) WITHOUT
    # consulting NO_PROXY at all. On a cluster with aiPlatform.proxy.enabled
    # (corporate outbound proxy for internet-bound pip installs/model
    # downloads), that silently routes the worker's *in-cluster* connection
    # to LIVEKIT_URL through the proxy too, which cannot reach a
    # cluster-internal Service hostname - the proxy returns some non-101
    # response and the WS handshake fails with a confusing
    # "Invalid response status" error. LiveKit's own signaling connection is
    # always in-cluster; it must never go through the outbound proxy.
    #
    # This alone is NOT sufficient, though - confirmed live: every per-job
    # room connection (this entrypoint's ctx.connect(), and any other
    # rtc.Room.connect() call) goes through livekit-agents' Rust FFI layer,
    # a separate code path from the Python/aiohttp worker-registration
    # connection above, with its own (env-var-based) proxy handling that
    # this http_proxy="" kwarg does not reach. It hit the exact same
    # failure mode (routed through Squid, which then 403s a cluster-
    # internal host) - crashing every job before STT/LLM/TTS ever ran.
    # The actual fix is in voice-agent.yaml: HTTP_PROXY/HTTPS_PROXY are
    # unset before `python agent.py start` runs, so neither this Python
    # layer nor the Rust FFI layer ever sees them - proxy env vars are
    # only needed during the earlier `pip install` step.
    #
    # agent_name is deliberately non-empty: an EMPTY agent_name makes
    # livekit-agents' automatic dispatch fire once per room CREATION (not
    # per participant join) - confirmed live, this silently drops every
    # session after the first for the dashboard's single static room name
    # ("sparc-argus-control-room"), since the room object stays alive for
    # emptyTimeout (300s) after emptying and a rejoin within that window
    # isn't a new "creation" from the SFU's point of view. Naming the
    # worker opts OUT of automatic dispatch entirely - it now only runs
    # jobs explicitly requested via AgentDispatchService.CreateDispatch,
    # which voice-token-server.py calls on every /token request, so every
    # session (fresh room or not) gets its own job.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, http_proxy="", agent_name="argus-voice-agent"))
