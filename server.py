"""Stdlib HTTP API so ROSTR can be an agent harness backend.

    python3 server.py            # 0.0.0.0:8787
    python3 server.py --port 8787

GET  /health
GET  /v1
GET  /v1/health
POST /v1/sessions              {"goal": "...", "scenario?": "fix-500", "scope?": "dev"}
POST /v1/sessions/{id}/turns   {"query": "...", "approve?": false}
GET  /v1/sessions/{id}
POST /v1/generate              {"system": "...", "body": "...", "route": "frontier"}
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Dict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from harness import HarnessSession

SESSIONS: dict[str, HarnessSession] = {}
VERSION = "0.4.0"
CONTRACT = {
    "ok": True,
    "name": "ROSTR Jev harness",
    "version": VERSION,
    "transport": "stdlib HTTP + Vercel AI Gateway",
    "endpoints": {
        "GET /v1/health": "Gateway health and contract",
        "POST /v1/sessions": "{ goal, scenario?, scope? } → session",
        "GET /v1/sessions/{id}": "Explicit state",
        "POST /v1/sessions/{id}/turns": "{ query, approve? } → decision + assembled context + action",
        "POST /v1/generate": "{ system, body, route } → generator via Vercel AI Gateway",
    },
    "decisions": [
        "context visibility: hide | short | long | full",
        "cache: reuse | rebuild (noul)",
        "route: frontier | subagent | cheap | background + cost",
        "tools: ranked top-k, snippet-first",
        "permissions: allow | ask | deny",
        "security: open | standard | restricted | custom",
    ],
}


def _request_path(handler: BaseHTTPRequestHandler) -> str:
    parsed = urlparse(handler.path)
    path = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)
    if qs.get("__path"):
        return qs["__path"][0].rstrip("/") or "/"
    if path.startswith("/api/"):
        path = "/" + path[5:]
        path = path.rstrip("/") or "/"
    sid = (qs.get("id") or [""])[0]
    mapping = {
        "/": "/v1/health",
        "/health": "/v1/health",
        "/index": "/v1/health",
        "/index.py": "/v1/health",
        "/sessions": "/v1/sessions",
        "/generate": "/v1/generate",
        "/session": f"/v1/sessions/{sid}",
        "/turns": f"/v1/sessions/{sid}/turns",
    }
    if path in mapping:
        return mapping[path]
    if path.startswith("/v1"):
        return path
    return path


def _json(handler, code, payload):
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "content-type,authorization")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _turn_payload(sid: str, sess: HarnessSession, result: Dict) -> Dict:
    action = result["action"]
    return {
        "ok": True,
        "sessionId": sid,
        "session": sess.to_public(sid),
        "decision": result["decision"],
        "assembled": result["assembled"],
        "action": action,
        "turn": sess.turn,
        "pendingAsk": sess.pending_ask,
        "blocked": bool(action.get("blocked")),
        "needsApproval": bool(sess.pending_ask),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type,authorization")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = _request_path(self)
        if path in ("/", "/health", "/v1", "/v1/health"):
            return _json(self, 200, {
                **CONTRACT,
                "gateway": {
                    "baseUrl": "https://ai-gateway.vercel.sh/v1",
                    "api_key_env": "AI_GATEWAY_API_KEY",
                },
            })
        if path.startswith("/v1/sessions/"):
            sid = path.split("/")[3]
            sess = SESSIONS.get(sid)
            if not sess:
                return _json(self, 404, {"ok": False, "error": "unknown session"})
            pub = sess.to_public(sid)
            return _json(self, 200, {
                "ok": True,
                "sessionId": sid,
                "session": pub,
                "id": sid,
                "goal": sess.goal,
                "turn": sess.turn,
                "pendingAsk": sess.pending_ask,
                "chunks": [
                    {k: c[k] for k in ("id", "kind", "title", "tokens", "path", "sensitivity", "rw") if k in c}
                    for c in sess.chunks
                ],
            })
        return _json(self, 404, {"ok": False, "error": "not found", "path": path, "raw": self.path})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return _json(self, 400, {"ok": False, "error": "invalid json"})
        path = _request_path(self)
        if path == "/v1/sessions":
            goal = (body.get("goal") or "").strip()
            if not goal:
                return _json(self, 400, {"ok": False, "error": "goal required"})
            sid = uuid.uuid4().hex[:8]
            SESSIONS[sid] = HarnessSession.create(
                goal,
                scenario=body.get("scenario") or "fix-500",
                scope=body.get("scope"),
            )
            pub = SESSIONS[sid].to_public(sid)
            return _json(self, 200, {"ok": True, "sessionId": sid, "session": pub, **{k: pub[k] for k in ("goal", "turn")}})
        if path.startswith("/v1/sessions/") and path.endswith("/turns"):
            sid = path.split("/")[3]
            sess = SESSIONS.get(sid)
            if not sess:
                return _json(self, 404, {"ok": False, "error": "unknown session"})
            query = (body.get("query") or sess.goal).strip()
            if not query:
                return _json(self, 400, {"ok": False, "error": "query required"})
            result = sess.run_turn(query, approve=bool(body.get("approve")))
            return _json(self, 200, _turn_payload(sid, sess, result))
        if path.startswith("/v1/sessions/") and not path.endswith("/turns"):
            sid = path.split("/")[3]
            sess = SESSIONS.get(sid)
            if not sess:
                return _json(self, 404, {"ok": False, "error": "unknown session"})
            query = (body.get("query") or "").strip()
            if not query:
                return _json(self, 400, {"ok": False, "error": "query required"})
            result = sess.run_turn(query, approve=bool(body.get("approve")))
            return _json(self, 200, _turn_payload(sid, sess, result))
        if path == "/v1/generate":
            from gateway import complete
            cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            with open(cfg_path, encoding="utf-8") as f:
                config = json.load(f)
            route = body.get("route", "frontier")
            gw = config["gateway"]
            model = {
                "cheap": gw.get("cheap_model"),
                "background": gw.get("background_model") or gw.get("cheap_model"),
                "subagent": gw.get("cheap_model"),
            }.get(route, gw.get("frontier_model") or gw["default_model"])
            try:
                text = complete(model, [
                    {"role": "system", "content": (body.get("system") or "")[:1500]},
                    {"role": "user", "content": (body.get("body") or "")[:6000]},
                ], config)
            except Exception as e:
                return _json(self, 502, {"ok": False, "error": str(e), "model": model})
            return _json(self, 200, {
                "ok": True,
                "generate": {"text": text, "model": model, "provider": "vercel-ai-gateway"},
            })
        return _json(self, 404, {"ok": False, "error": "not found", "path": path, "raw": self.path})


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
