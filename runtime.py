"""The runtime loop.

Two functions:
  run_worker(manifest, ...) — ONE agent working: PAL rebuilds the prompt every
      iteration -> LLM picks a JSON action -> tool runs -> result recorded ->
      repeat until done / max steps.
  run_master(objective, ...) — the orchestrator: decompose -> NPAO classify ->
      order N->A->P->O -> run a worker per task -> verify -> log decisions.

The runtime is deliberately thin. All the smarts live in PAL (what the agent
is told), NPAO (what it does next), the hub (what it remembers), and the
tools (what it can touch).
"""
import json
import os

import npao
import pal as pal_mod

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


def run_worker(manifest, hub, tools, config, run_id, verbose=True, mock_tag="worker"):
    """Run one worker to completion. Returns {"status", "result", "steps"}."""
    from gateway import complete  # local import: keeps `import runtime` light

    agent_type = manifest["runtime"]["agent_type"]
    model = manifest["runtime"]["model"]
    max_steps = manifest["runtime"].get(
        "max_steps", config["runtime"]["max_steps_per_worker"])
    system = _read_agent_file("worker.md")
    history = []

    for step in range(max_steps):
        # THE key line: PAL rebuilds the prompt every iteration with fresh state.
        prompt = pal_mod.build_step_prompt(manifest, history)
        raw = complete(model,
                       [{"role": "system", "content": system},
                        {"role": "user", "content": prompt}],
                       config, mock_tag=mock_tag)
        try:
            act = _extract_json(raw)
        except ValueError:
            # One reformat retry, then stop cleanly with what we have.
            raw = complete(model,
                           [{"role": "system", "content": system},
                            {"role": "user", "content": prompt + "\n\nReply with JSON only."}],
                           config, mock_tag=mock_tag)
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
            return {"status": "done", "result": act.get("result"), "steps": step + 1}

        action, args = act.get("action", ""), act.get("args", {})
        res = tools.call(action, args)
        hub.log_step(run_id, agent_type, thought, action, args, res)
        history.append({"thought": thought, "action": action, "result": res})
        if verbose:
            ok = res.get("ok")
            print(f"  [step {step + 1}] {thought[:70]} -> {action} (ok={ok})")

    return {"status": "max_steps", "result": history[-1] if history else None,
            "steps": max_steps}


def run_master(objective, hub, tools, config, verbose=True):
    """Orchestrate one objective end-to-end. Returns {"run_id", "results"}."""
    from gateway import complete

    run_id = hub.create_run(objective)
    system = _read_agent_file("master.md")

    # 1. Decompose into subtasks.
    raw = complete(
        config["gateway"]["cheap_model"],
        [{"role": "system", "content": system},
         {"role": "user",
          "content": f"OBJECTIVE: {objective}\n\n"
                     "Decompose into 2-4 concrete subtasks. "
                     'Reply JSON ONLY: {"subtasks": [{"text": "..."}]}'}],
        config, mock_tag="master:decompose")
    subtasks = _extract_json(raw)["subtasks"]

    # 2. NPAO classify + order N -> A -> P -> O.
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

    # 3. A worker per task, in order. (Parallel fan-out is the extension point.)
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

    # 4. Report.
    done = sum(1 for r in results if r["status"] == "done")
    hub.log_decision(run_id, f"{done}/{len(results)} tasks done",
                     "master verification pass")
    if verbose:
        print(f"\n[master] finished: {done}/{len(results)} tasks done "
              f"(run {run_id})")
    return {"run_id": run_id, "results": results}


# ---------------------------------------------------------------------------
# EXTENSION POINT: parallel workers
# Replace the sequential loop in step 3 with a ThreadPoolExecutor over
# run_worker calls (one Hub/ToolRegistry per thread, or add locking).
# Keep NPAO order for NECESSITY tasks — they block everything else.
# ---------------------------------------------------------------------------
