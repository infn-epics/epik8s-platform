"""
Minimal LiveKit token-minting endpoint (experimental voice assistant).

POST {"room": "<name>", "identity": "<id>"} -> {"token": "<jwt>"}
matches exactly what epik8s-dashboard's VoiceRoomClient._fetchToken()
expects (src/services/voiceRoom.js) - the `?voiceToken=` dashboard config
should point here.

Deliberately reimplements the LiveKit access-token JWT format with PyJWT
directly rather than depending on the `livekit-api`/`livekit` Python SDK -
one fewer dependency for a service this small. The format (HS256, `iss`=
API key, `sub`=identity, a `video` grant claim) is LiveKit's stable,
documented access-token shape.

No authentication on this endpoint beyond "the caller names a room" -
anyone who can reach it can mint a token to join any room with full
publish/subscribe/data rights. Acceptable for an experimental,
not-yet-linked-from-anywhere-public endpoint; revisit (e.g. require the
dashboard's existing AuthContext session, or a shared secret header)
before treating this as production-hardened. See platform README's
"Voice Assistant" section.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt

API_KEY = os.environ["LIVEKIT_API_KEY"]
API_SECRET = os.environ["LIVEKIT_API_SECRET"]
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "21600"))  # 6h
PORT = int(os.environ.get("PORT", "8080"))


def mint_token(room: str, identity: str) -> str:
    now = int(time.time())
    payload = {
        "iss": API_KEY,
        "sub": identity,
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    return jwt.encode(payload, API_SECRET, algorithm="HS256")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:  # quiet default access log
        pass

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # CORS preflight - the dashboard calls this cross-origin
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/token":
            self._send_json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return

        room = (body.get("room") or "").strip()
        identity = (body.get("identity") or "").strip() or f"operator-{uuid.uuid4().hex[:8]}"
        if not room:
            self._send_json(400, {"error": "missing_room"})
            return

        self._send_json(200, {"token": mint_token(room, identity)})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[voice-token] listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
