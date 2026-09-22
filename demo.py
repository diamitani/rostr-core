"""Rostr-core demo: master + workers through the Jev harness.

Usage:
    python3 demo.py           # mock — no API key, no spend
    python3 demo.py --live    # Vercel AI Gateway (AI_GATEWAY_API_KEY)
    python3 demo.py --harness # print one assembled turn and exit
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

if "--live" not in sys.argv:
    os.environ["ROSTR_MOCK"] = "1"
    os.environ.setdefault("ROSTR_AUTO_APPROVE", "1")
    print("MOCK MODE — no API key, no spend. Add --live for a real gateway run.\n")
else:
    print("LIVE MODE — calling Vercel AI Gateway.\n")

from hub import Hub
from tools import default_registry
from runtime import run_master
from harness import HarnessSession

config = json.load(open("config.json"))
config["hub"]["dir"] = os.path.join(BASE, ".rostr")

if "--harness" in sys.argv:
    s = HarnessSession("Fix error 500 in chat stream handler")
    s.add("file", ".env.local", "OPENAI_API_KEY=sk-live", path=".env.local",
          sensitivity="restricted")
    s.add("file", "src/chat/stream.ts",
          "export async function streamChat(req) { JSON.parse(body) }",
          path="src/chat/stream.ts")
    d = s.decide("Where is the 500 coming from in the chat stream?")
    print("route:", d["route"]["target"])
    print("permission:", d["permission"]["action"], d["permission"]["reason"])
    print("disclosed tools:", d["tools"]["disclosed"])
    print("cost trap:", d["cost"]["trap"],
          "frontier", d["cost"]["naiveFrontier"],
          "naive mix", d["cost"]["naiveRouted"],
          "harness", d["cost"]["harness"])
    print("\n--- assembled ---\n")
    print(s.assemble(d))
    sys.exit(0)

hub = Hub(config["hub"]["dir"])
tools = default_registry(config)
hub.register_agent("master-1", {"type": "orchestrator",
                                "model": config["gateway"]["cheap_model"]})

objective = "Prepare a launch checklist for artist Copperline's new single"
print(f"OBJECTIVE: {objective}\n")

out = run_master(objective, hub, tools, config, verbose=True)

print("\n=== RUN SUMMARY ===")
for r in out["results"]:
    print(f"  [{r['npao']}] {r['task'][:60]} -> {r['status']} ({r['steps']} steps)")
print(f"\nHub data: {config['hub']['dir']}/ (registry.json, state.json, reference.jsonl)")
