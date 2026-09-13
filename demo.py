"""Rostr-core demo: master + workers complete a toy task in the terminal.

Usage:
    python3 demo.py           # mock mode — no API key, no spend, runs now
    python3 demo.py --live    # real run via Vercel AI Gateway (AI_GATEWAY_API_KEY)

Mock mode prints every loop iteration so you can SEE the machinery:
PAL compiling, NPAO ordering, the worker's thought -> action -> result loop,
and everything landing in the hub (.rostr/).
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

if "--live" not in sys.argv:
    os.environ["ROSTR_MOCK"] = "1"
    print("MOCK MODE — no API key, no spend. Add --live for a real gateway run.\n")
else:
    print("LIVE MODE — calling Vercel AI Gateway.\n")

from hub import Hub
from tools import default_registry
from runtime import run_master

config = json.load(open("config.json"))
config["hub"]["dir"] = os.path.join(BASE, ".rostr")

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
print("Try: cat .rostr/state.json | python3 -m json.tool | head -60")
