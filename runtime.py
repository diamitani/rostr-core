"""The runtime loop.

Two functions:
  run_worker(manifest, ...) — ONE agent working. The Jev harness scores every
      chunk, discloses tools in tiers, prices routing, then the model sees an
      assembled context — not an append-only transcript.
  run_master(objective, ...) — orchestrator: decompose -> NPAO classify ->
      order N->A->P->O -> run a worker per task -> verify -> log decisions.
"""
import json
import os

import npao
import pal as pal_mod
from harness import HarnessSession

_BASE = os.path.dirname(os.path.abspath(__file__))


def _read_agent_file(name):
    with open(os.path.join(_BASE, "agents", name), "r", encoding="utf-8") as f:
        return f.read()


def _extract_json(text):
    s = text.find("{")
    e = text.rfind("}")
    if s < 0 or e < 0 or e <= s:
        raise ValueError("no JSON object found")
    return json.loads(text[s:e + 1])


def _pick_model(decision, config, fallback):
    from gateway import model_for, resolve_gateway
    resolved = resolve_gateway(config)
    target = decision["route"]["target"]
    return model_for(target, resolved["models"]) or fallback


def run_worker(manifest, hub, tools, config, run_id, verbose=True, mock_tag="worker"):
    """Run one worker to completion. Returns {"status", "result", "steps"}."""
    from gateway import complete

    agent_type = manifest["runtime"]["agent_type"]
    default_model = manifest["runtime"]["model"]
    max_steps = manifest["runtime"].get(
        "max_steps", config["runtime"]["max_steps_per_worker"])
    system = _read_agent_file("worker.md")
    session = HarnessSession.from_manifest(manifest)
    query = manifest["instructions"]["task_description"]
    auto = os.environ.get("ROSTR_AUTO_APPROVE", "") == "1"

    for step in range(max_steps):
        decision = session.decide(query)
        perm = decision["permission"]["action"]
        if perm == "deny":
            hub.log_decision(run_id, "deny " + decision["permission"]["command"],
                             decision["permission"]["reason"])
            return {"status": "denied", "result": decision["permission"]["reason"],
                    "steps": step, "decision": decision}
        if perm == "ask" and not auto:
            hub.log_decision(run_id, "ask " + str(decision["permission"].get("command")),
                             decision["permission"]["reason"])
            if verbose:
                print(f"  [step {step + 1}] ASK: {decision['permission']['reason']}")
            return {"status": "needs_approval", "result": decision["permission"],
                    "steps": step, "decision": decision}

        prompt = session.assemble(decision)
        model = _pick_model(decision, config, default_model)
        route = decision["route"]["target"]
        hub.log_decision(
            run_id,
            f"route={route} tools={decision['tools']['disclosed']}",
            f"assembled {decision['cost']['xSmallTokens']} tok vs state {decision['cost']['xTokens']}",
        )
        raw = complete(model,
                       [{"role": "system", "content": system},
                        {"role": "user", "content": prompt}],
                       config, mock_tag=mock_tag, route=route)
        try:
            act = _extract_json(raw)
        except ValueError:
            raw = complete(model,
                           [{"role": "system", "content": system},
                            {"role": "user", "content": prompt + "\n\nReply with JSON only."}],
                           config, mock_tag=mock_tag, route=route)
            try:
                act = _extract_json(raw)
            except ValueError:
                hub.log_step(run_id, agent_type, "parse failed", "error", {}, raw[:300])
                return {"status": "parse_failed", "result": raw[:500], "steps": step + 1}

        thought = act.get("thought", "")
        if act.get("done"):
            hub.log_step(run_id, agent_type, thought, "done", {}, act.get("result"))
            if verbose:
                print(f"  [step {step + 1}] done: {str(act.get('result'))[:100]}")
            return {"status": "done", "result": act.get("result"), "steps": step + 1,
                    "decision": decision}

        action, args = act.get("action", ""), act.get("args", {})
        res = tools.call(action, args)
        session.record_tool(action, args, res)
        hub.log_step(run_id, agent_type, thought, action, args, res)
        if verbose:
            ok = res.get("ok")
            vis = sum(1 for v in decision["visibility"] if v["visibility"] != "hide")
            print(f"  [step {step + 1}] {thought[:60]} -> {action} (ok={ok}) "
                  f"[{decision['route']['target']}, {vis} chunks visible]")

    return {"status": "max_steps", "result": None, "steps": max_steps}


def run_master(objective, hub, tools, config, verbose=True):
    """Orchestrate one objective end-to-end. Returns {"run_id", "results"}."""
    from gateway import complete

    run_id = hub.create_run(objective)
    system = _read_agent_file("master.md")

    raw = complete(
        config["gateway"]["cheap_model"],
        [{"role": "system", "content": system},
         {"role": "user",
          "content": f"OBJECTIVE: {objective}\n\n"
                     "Decompose into 2-4 concrete subtasks. "
                     'Reply JSON ONLY: {"subtasks": [{"text": "..."}]}'}],
        config, mock_tag="master:decompose")
    subtasks = _extract_json(raw)["subtasks"]

    tasks = []
    for s in subtasks:
        cls, reason = npao.classify(s["text"])
        idx = hub.add_task(run_id, s["text"], cls, reason)
        tasks.append({"idx": idx, "text": s["text"], "npao": cls,
                      "reason": reason, "skill": s.get("skill")})
    tasks = npao.order(tasks)
    hub.log_decision(run_id,
                     f"execution order: {[t['npao'] for t in tasks]}",
                     "NPAO triage — Necessity, then Anxiety, then Priority, then Opportunity")

    results = []
    for i, t in enumerate(tasks):
        if verbose:
            print(f"\n[master] task {i + 1}/{len(tasks)} [{t['npao']}] {t['text']}"
                  f"\n         why: {t['reason']}")
        manifest, _ = pal_mod.compile(
            t["text"], skill_path=t["skill"], hub=hub,
            model=config["gateway"]["default_model"],
            tools_policy=config["tools"])
        hub.log_decision(run_id, f"worker {i} <- {t['text'][:60]}",
                         f"PAL compiled manifest (ambiguity={manifest['_meta']['ambiguity']})")
        r = run_worker(manifest, hub, tools, config, run_id,
                       verbose=verbose, mock_tag=f"worker:{i}")
        hub.update_task(run_id, t["idx"],
                        "done" if r["status"] == "done" else r["status"],
                        r.get("result"))
        results.append({"task": t["text"], "npao": t["npao"], **r})

    done = sum(1 for r in results if r["status"] == "done")
    hub.log_decision(run_id, f"{done}/{len(results)} tasks done",
                     "master verification pass")
    if verbose:
        print(f"\n[master] finished: {done}/{len(results)} tasks done "
              f"(run {run_id})")
    return {"run_id": run_id, "results": results}
