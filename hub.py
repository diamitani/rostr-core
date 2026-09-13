"""Hub: agent registry + run state + reference knowledge.

v1 is JSON-file backed (zero setup — works the moment you run it).
The Supabase adapter is a documented drop-in; see WIRING.md for the schema.

On disk (.rostr/):
  registry.json   — which agents exist and what they can do
  state.json      — runs, tasks, and step history
  reference.jsonl — append-only learnings / decisions / knowledge
"""
import json
import os
import time
import uuid


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Hub:
    def __init__(self, root=".rostr"):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._registry_p = os.path.join(root, "registry.json")
        self._state_p = os.path.join(root, "state.json")
        self._ref_p = os.path.join(root, "reference.jsonl")
        self._registry = self._load(self._registry_p, {})
        self._state = self._load(self._state_p, {"runs": {}})

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _load(path, default):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return default

    def _save_registry(self):
        with open(self._registry_p, "w", encoding="utf-8") as f:
            json.dump(self._registry, f, indent=2)

    def _save_state(self):
        with open(self._state_p, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2)

    # -- registry: which agents exist ---------------------------------------
    def register_agent(self, agent_id, spec):
        """spec: {type, model, capabilities, skills, ...}"""
        spec = dict(spec)
        spec["registered_at"] = _now()
        self._registry[agent_id] = spec
        self._save_registry()
        return agent_id

    def list_agents(self):
        return self._registry

    # -- state: runs, tasks, steps -------------------------------------------
    def create_run(self, objective):
        run_id = uuid.uuid4().hex[:8]
        self._state["runs"][run_id] = {
            "objective": objective,
            "created_at": _now(),
            "tasks": [],
            "steps": [],
            "decisions": [],
        }
        self._save_state()
        return run_id

    def add_task(self, run_id, text, npao_class, reason=""):
        run = self._state["runs"][run_id]
        task = {"text": text, "npao": npao_class, "reason": reason,
                "status": "queued", "result": None}
        run["tasks"].append(task)
        self._save_state()
        return len(run["tasks"]) - 1

    def update_task(self, run_id, idx, status, result=None):
        task = self._state["runs"][run_id]["tasks"][idx]
        task["status"] = status
        task["result"] = result
        self._save_state()

    def log_step(self, run_id, agent, thought, action, args, result):
        self._state["runs"][run_id]["steps"].append({
            "t": _now(), "agent": agent, "thought": thought,
            "action": action, "args": args, "result": result,
        })
        self._save_state()

    def log_decision(self, run_id, choice, reasoning):
        """Append-only DECISIONS log: what was chosen + why."""
        self._state["runs"][run_id]["decisions"].append(
            {"t": _now(), "choice": choice, "reasoning": reasoning})
        self._save_state()

    # -- reference: long-term knowledge --------------------------------------
    def log_learning(self, kind, text, tags=None):
        """kind: learning | decision | knowledge. Append-only."""
        entry = {"t": _now(), "kind": kind, "text": text, "tags": tags or []}
        with open(self._ref_p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def search_reference(self, query, limit=3):
        """v1: keyword match. v2: replace with vector search (pgvector)."""
        if not os.path.exists(self._ref_p):
            return []
        terms = [w.lower() for w in query.split() if len(w) > 2]
        scored = []
        with open(self._ref_p, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                hay = (e.get("text", "") + " " + " ".join(e.get("tags", []))).lower()
                score = sum(hay.count(t) for t in terms)
                if score:
                    scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:limit]]


# ---------------------------------------------------------------------------
# EXTENSION POINT: SupabaseHub(Hub)
# Subclass and override _load/_save_*/log_learning/search_reference to use
# Supabase (Postgres + pgvector). Schema is in WIRING.md. The runtime only
# calls the public methods above, so the swap is invisible to it.
# ---------------------------------------------------------------------------
