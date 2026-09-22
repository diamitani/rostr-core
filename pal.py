"""PAL — Prompt Abstraction Layer.

Compiles sloppy intent into a structured agent manifest + the prompt the
runtime sends every loop iteration.

v1 does template assembly + rule-based sharpening (no LLM call needed to
compile). The LLM-powered enhancement pass is a marked upgrade path.

Five stages (per the paper):
  1. extract  — normalize the intent, score ambiguity
  2. inject   — pull relevant context from the hub
  3. enhance  — strip hedging, add missing precision
  4. compile  — emit the typed manifest (dict; YAML/JSON-serializable)
  5. route    — pick the agent type
"""
import os

HEDGES = ["maybe ", "perhaps ", "i think we should ", "could we ",
          "might be good to ", "it would be nice to "]


def _ambiguity(intent):
    """v1 heuristic: 1.0 - (explicit params / required params). Upgrade: LLM."""
    text = intent.lower()
    score = 0.7  # default: most raw intents are underspecified
    if any(w in text for w in ("for ", "to ", "in ")):
        score -= 0.15
    if len(text.split()) > 12:
        score -= 0.15
    if "?" in text:
        score += 0.1
    return round(max(0.0, min(1.0, score)), 2)


def _enhance(intent):
    """Rule-based sharpening. Upgrade path: one cheap LLM call here."""
    out = intent.strip()
    low = out.lower()
    for h in HEDGES:
        if low.startswith(h):
            out = out[len(h):]
            low = out.lower()
            break
    out = out[0].upper() + out[1:] if out else out
    if not out.endswith((".", "!", "?", ":")):
        out += "."
    return out


def _route(intent):
    import jev
    return jev.classify_agent(intent)


def compile(intent, skill_path=None, hub=None, model=None,
            tools_policy=None, extra_context=""):
    """Full pipeline. Returns a manifest dict (the paper's YAML, as JSON)."""
    # Stage 1 — extract
    ambiguity = _ambiguity(intent)

    # Stage 2 — inject context from the hub
    context_bits = []
    if extra_context:
        context_bits.append(extra_context)
    if hub is not None:
        for e in hub.search_reference(intent):
            context_bits.append(f"[{e.get('kind')}] {e.get('text')}")

    # Stage 2b — inject the Context Engine pack (session memory).
    # Active when a storage backend is configured (ROSTR_STORAGE_PATH or
    # SUPABASE_URL); falls back silently to the hub-only behavior above.
    kb_pack = ""
    try:
        from context_engine import brain_index_from_env, assemble_pack
        index = brain_index_from_env()
        if index is not None:
            project_id = os.environ.get("ROSTR_PROJECT_ID", "default")
            pack = assemble_pack(intent, project_id, hub=hub, index=index)
            kb_pack = pack["pack_text"]
    except Exception:
        kb_pack = ""  # storage misconfigured or unreachable: never break a run
    if kb_pack:
        context_bits.append(kb_pack)

    # Stage 3 — enhance
    task = _enhance(intent)

    # Stage 4 — compile the manifest
    skill_text = ""
    if skill_path and os.path.exists(skill_path):
        with open(skill_path, "r", encoding="utf-8") as f:
            skill_text = f.read()
    policy = tools_policy or {"allow": [], "deny": []}
    agent_type = _route(intent)  # Stage 5 — route

    manifest = {
        "runtime": {
            "agent_type": agent_type,
            "model": model or "anthropic/claude-sonnet-4-6",
            "temperature": 0.2,
            "max_steps": 12,
        },
        "instructions": {
            "task_description": task,
            "completion_criteria": [
                "The task_description is fully carried out.",
                "Every file or artifact mentioned exists and is readable.",
                "No invented facts: mark placeholders as placeholders.",
            ],
            "escalation_policy": "require-approval",
        },
        "tools_enabled": {
            "allow": list(policy.get("allow", [])),
            "deny": list(policy.get("deny", [])),
        },
        "memory": {
            "mode": "project",
            "context_sources": ["reference"],
            "save_triggers": ["decisions", "learnings", "artifacts"],
        },
        "_meta": {
            "raw_intent": intent,
            "ambiguity": ambiguity,
            "skill_path": skill_path,
        },
    }

    prompt = _build_prompt(manifest, skill_text, context_bits, history=[])
    return manifest, prompt


def _build_prompt(manifest, skill_text, context_bits, history, max_history=6):
    """Assemble the prompt the runtime sends on EVERY loop iteration."""
    ins = manifest["instructions"]
    tools = manifest["tools_enabled"]
    lines = [
        f"You are a {manifest['runtime']['agent_type']} agent.",
        "",
        f"TASK: {ins['task_description']}",
        "",
        "COMPLETION CRITERIA:",
    ]
    lines += [f"  - {c}" for c in ins["completion_criteria"]]
    lines += [
        "",
        f"TOOLS YOU MAY USE: {', '.join(tools['allow']) or '(none)'}",
        f"TOOLS FORBIDDEN: {', '.join(tools['deny']) or '(none)'}",
        "Anything that sends, posts, or spends requires approval — draft it, do not execute.",
    ]
    if skill_text:
        lines += ["", "SKILL (follow these steps):", skill_text.strip()[:4000]]
    if context_bits:
        lines += ["", "RELEVANT CONTEXT FROM THE HUB:"]
        lines += [f"  - {b[:300]}" for b in context_bits[:5]]
    if history:
        lines += ["", "WHAT HAS HAPPENED SO FAR:"]
        for h in history[-max_history:]:
            res = h.get("result", {})
            summary = res.get("result", res.get("error", "")) if isinstance(res, dict) else res
            lines.append(f"  - thought: {h.get('thought','')[:120]}")
            lines.append(f"    action: {h.get('action')} -> {str(summary)[:160]}")
    lines += [
        "",
        "Reply with JSON ONLY, one of:",
        '  {"thought": "...", "action": "<tool_name>", "args": {...}}',
        '  {"thought": "...", "done": true, "result": "<what was accomplished>"}',
        "Do not ask questions — make reasonable assumptions and note them in thought.",
    ]
    return "\n".join(lines)


def build_step_prompt(manifest, history):
    """Rebuild the prompt mid-loop with fresh history. The runtime calls this
    every iteration — this is where PAL integrates into the runtime."""
    return _build_prompt(manifest, "", [], history)


def compile_agent(description, template_path=None):
    """Agent factory: natural-language description -> agent definition file
    content. v1 fills the worker template; the paper's full spec generation
    (tools inference, triggers) is the upgrade path."""
    base = os.path.dirname(os.path.abspath(__file__))
    tpl = template_path or os.path.join(base, "agents", "worker.md")
    with open(tpl, "r", encoding="utf-8") as f:
        template = f.read()
    name = description.strip().split("\n")[0][:60]
    return f"# Agent: {name}\n\nOriginal request: {description.strip()}\n\n" + template
