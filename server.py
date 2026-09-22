"""Stdlib HTTP API so ROSTR can be an agent harness backend.

    python3 server.py            # 0.0.0.0:8787
    python3 server.py --port 8787

GET  /health
POST /v1/sessions          {"goal": "..."}
POST /v1/sessions/{id}/turns   {"query": "..."}
GET  /v1/sessions/{id}
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from harness import HarnessSession

SESSIONS: dict[str, HarnessSession] = {}


def _json(handler, code, payload):
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(raw)


def _public(sess: HarnessSession, sid: str) -> dict:
    return {
        "id": sid,
        "goal": sess.goal,
        "turn": sess.turn,
        "chunks": [{"id": c["id"], "kind": c["kind"], "title": c["title"],
                    "tokens": c["tokens"], "path": c.get("path"),
                    "sensitivity": c["sensitivity"]} for c in sess.chunks],
        "pendingAsk": sess.pending_ask,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/health"):
            return _json(self, 200, {
                "ok": True,
                "name": "ROSTR Jev harness",
                "version": "0.2.0",
                "endpoints": [
                    "POST /v1/sessions",
                    "POST /v1/sessions/{id}/turns",
                    "GET /v1/sessions/{id}",
                ],
            })
        if path.startswith("/v1/sessions/"):
            sid = path.split("/")[3]
            sess = SESSIONS.get(sid)
            if not sess:
                return _json(self, 404, {"ok": False, "error": "unknown session"})
            return _json(self, 200, {"ok": True, **_public(sess, sid)})
        return _json(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return _json(self, 400, {"ok": False, "error": "invalid json"})
        path = urlparse(self.path).path.rstrip("/")
        if path == "/v1/sessions":
            goal = (body.get("goal") or "").strip()
            if not goal:
                return _json(self, 400, {"ok": False, "error": "goal required"})
            sid = uuid.uuid4().hex[:8]
            SESSIONS[sid] = HarnessSession(goal, scope=body.get("scope", "dev"))
            return _json(self, 200, {"ok": True, "sessionId": sid, **_public(SESSIONS[sid], sid)})
        if path.startswith("/v1/sessions/") and path.endswith("/turns"):
            sid = path.split("/")[3]
            sess = SESSIONS.get(sid)
            if not sess:
                return _json(self, 404, {"ok": False, "error": "unknown session"})
            query = (body.get("query") or sess.goal).strip()
            decision = sess.decide(query)
            assembled = sess.assemble(decision)
            if decision["permission"]["action"] == "deny":
                return _json(self, 200, {
                    "ok": True, "blocked": True, "decision": decision,
                    "assembledTokens": jev_tokens(assembled),
                })
            if decision["permission"]["action"] == "ask" and not body.get("approve"):
                sess.pending_ask = decision["permission"]
                return _json(self, 200, {
                    "ok": True, "blocked": True, "needsApproval": True,
                    "decision": decision, "assembled": assembled,
                })
            return _json(self, 200, {
                "ok": True,
                "blocked": False,
                "decision": _strip_vis(decision),
                "assembled": assembled,
                "assembledTokens": jev_tokens(assembled),
                "session": _public(sess, sid),
            })
        return _json(self, 404, {"ok": False, "error": "not found"})


def jev_tokens(text: str) -> int:
    import jev
    return jev.tokens(text)


def _strip_vis(decision: Dict) -> Dict:
    d = dict(decision)
    d["visibility"] = [
        {k: v for k, v in item.items() if k != "excerpt"}
        for item in decision.get("visibility", [])
    ]
    return d


def main():
    port = 8787
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    port = int(os.environ.get("PORT", port))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"ROSTR harness backend on 0.0.0.0:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
