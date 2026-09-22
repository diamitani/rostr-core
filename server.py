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
from typing import Dict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from harness import HarnessSession

SESSIONS: dict[str, HarnessSession] = {}


def _request_path(handler: BaseHTTPRequestHandler) -> str:
    parsed = urlparse(handler.path)
    path = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)
    if qs.get("__path"):
        return qs["__path"][0].rstrip("/") or "/"
    for header in (
        "x-invoke-path",
        "x-forwarded-uri",
        "x-vercel-original-path",
        "x-matched-path",
        "x-rewrite-path",
        "x-real-url",
        "x-original-uri",
        "x-forwarded-path",
    ):
        raw = handler.headers.get(header)
        if raw:
            return urlparse(raw).path.rstrip("/") or "/"
    # Vercel sometimes puts the original URL in x-forwarded-url / referer-less host+path
    xf = handler.headers.get("x-forwarded-url") or handler.headers.get("x-url")
    if xf:
        return urlparse(xf).path.rstrip("/") or "/"
    aliases = {"/api": "/", "/api/index": "/", "/api/index.py": "/"}
    if path in aliases:
        return aliases[path]
    if path.startswith("/api/"):
        return path[4:].rstrip("/") or "/"
    return path



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
        path = _request_path(self)
        if path in ("/", "/health", "/v1", "/v1/health"):
            return _json(self, 200, {
                "ok": True,
                "name": "ROSTR Jev harness",
                "version": "0.3.0",
                "gateway": {
                    "baseUrl": "https://ai-gateway.vercel.sh/v1",
                    "api_key_env": "AI_GATEWAY_API_KEY",
                },
                "endpoints": [
                    "GET /v1/health",
                    "POST /v1/sessions",
                    "POST /v1/sessions/{id}/turns",
                    "GET /v1/sessions/{id}",
                    "POST /v1/generate",
                ],
            })
        if path.startswith("/v1/sessions/"):
            sid = path.split("/")[3]
            sess = SESSIONS.get(sid)
            if not sess:
                return _json(self, 404, {"ok": False, "error": "unknown session"})
            return _json(self, 200, {"ok": True, **_public(sess, sid)})
        return _json(self, 404, {
            "ok": False,
            "error": "not found",
            "path": path,
            "raw": self.path,
            "headers": {k: v for k, v in self.headers.items()
                        if k.lower() in (
                            "host", "x-invoke-path", "x-forwarded-uri", "x-forwarded-url",
                            "x-matched-path", "x-vercel-id", "x-real-url", "x-original-uri",
                            "x-forwarded-path", "x-rewrite-path", "x-vercel-original-path",
                            "x-forwarded-host", "x-invoke-query",
                        )},
        })

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
        return _json(self, 404, {
            "ok": False,
            "error": "not found",
            "path": path,
            "raw": self.path,
            "headers": {k: v for k, v in self.headers.items()
                        if k.lower() in (
                            "host", "x-invoke-path", "x-forwarded-uri", "x-forwarded-url",
                            "x-matched-path", "x-vercel-id", "x-real-url", "x-original-uri",
                            "x-forwarded-path", "x-rewrite-path", "x-vercel-original-path",
                            "x-forwarded-host", "x-invoke-query",
                        )},
        })


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
