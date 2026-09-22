"""System One stand-in for ROSTR.

Same contract as TypeSafe Jev: unstructured state in, typed Choice / Noul /
Score out. Swap the body of `decide()` for typesafe_sdk when TYPESAFE_API_KEY
is set. The local engine is evidence-weighted softmax — not Jev, not an LLM.

Official SDK (today):
    pip install typesafe-sdk
    export TYPESAFE_API_KEY=...   # console.typesafe.ai/keys — no waitlist
    POST https://api.typesafe.ai/v1/systemone
"""
from __future__ import annotations

import math
import os
import re
from typing import Dict, Iterable, List, Tuple


def _clamp(n: float) -> float:
    return max(0.0, min(1.0, n))


def softmax(weights: Dict[str, float], temperature: float = 0.7) -> Dict[str, float]:
    if not weights:
        return {}
    mx = max(weights.values())
    exps = {k: math.exp((v - mx) / temperature) for k, v in weights.items()}
    s = sum(exps.values()) or 1.0
    return {k: v / s for k, v in exps.items()}


def distribution(weights: Dict[str, float]) -> Dict:
    probs = softmax(weights)
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    choice = ranked[0][0] if ranked else next(iter(weights), "")
    top = ranked[0][1] if ranked else 0.0
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    mass = sum(weights.values())
    conf = _clamp((top - second) * 1.7 + min(mass / 10.0, 0.2))
    return {"choice": choice, "probabilities": probs, "confidence": round(conf, 4)}


def noul(yes: float, no: float) -> Dict:
    p = _clamp(yes / (yes + no + 1e-6))
    return {"noul": round(p, 3), "label": "yes" if p >= 0.5 else "no"}


def _hit(text: str, pattern: str, weight: float) -> float:
    return weight * min(len(re.findall(pattern, text, flags=re.I)), 4)


def lexical_overlap(query: str, text: str) -> float:
    stop = {"the", "a", "an", "to", "of", "and", "in", "on", "for", "is", "it", "this", "that"}
    q = [w for w in re.split(r"[^a-z0-9_./-]+", query.lower()) if len(w) > 2 and w not in stop]
    if not q:
        return 0.0
    hay = text.lower()
    return sum(1 for w in q if w in hay) / len(q)


def heat_lines(body: str, query: str) -> List[Dict]:
    return [{"line": ln, "score": round(lexical_overlap(query, ln), 3)} for ln in body.splitlines()]


def excerpt_from_heat(body: str, query: str, visibility: str) -> Tuple[str, List[Dict]]:
    heat = heat_lines(body, query)
    if visibility == "hide":
        return "", heat
    if visibility == "full":
        return body, heat
    thr = 0.22 if visibility == "short" else 0.08
    cap = 12 if visibility == "short" else 40
    kept = [h for h in heat if h["score"] >= thr]
    if not kept:
        kept = sorted(heat, key=lambda h: -h["score"])[: 4 if visibility == "short" else 12]
    return "\n".join(h["line"] for h in kept[:cap]), heat


def classify_phase(intent: str) -> Dict:
    t = intent
    w = {
        "PreD": 0.4 + _hit(t, r"\b(research|feasib|should we|whether to|go/no-go)\b", 3.6),
        "Design": 0.3 + _hit(t, r"\b(wireframe|figma|design tokens?|ux|ui spec)\b", 3.2),
        "Development": 0.4 + _hit(t, r"\b(implement|scaffold|endpoint|api route|build)\b", 2.4),
        "Deploy": 0.2 + _hit(t, r"\b(deploy|rollout|release|ship to prod|vercel)\b", 3.4),
        "Debugging": 0.3 + _hit(t, r"\b(fix|hotfix|bug|crash|error\s*\d{3}|stack\s*trace)\b", 3.4),
    }
    if re.search(r"\b(research|should we|whether to|feasib)\b", t, re.I):
        w["PreD"] += 2.4
    return distribution(w)


def classify_npao(text: str, blocks: Iterable = ()) -> Tuple[str, str]:
    t = text.lower()
    if blocks or any(w in t for w in ("must first", "blocked by", "depends on", "prerequisite")):
        return "NECESSITY", "blocks downstream work — resolve first"
    w = {
        "NECESSITY": 0.2 + _hit(t, r"\b(blocker|outage|sev[01]|p0|cannot ship)\b", 3.8),
        "ANXIETY": 0.3 + _hit(t, r"\b(fix|bug|error|broken|debt|flaky|cleanup)\b", 2.6),
        "PRIORITY": 0.5 + _hit(t, r"\b(must|need to|mission|this sprint|ship)\b", 2.0),
        "OPPORTUNITY": 0.2 + _hit(t, r"\b(nice to have|explore|someday|could also)\b", 3.2),
    }
    d = distribution(w)
    reasons = {
        "NECESSITY": "blocks downstream work — resolve first",
        "ANXIETY": "unresolved friction degrades everything else",
        "PRIORITY": "mission work (default class)",
        "OPPORTUNITY": "optional growth — only with spare capacity",
    }
    return d["choice"], reasons[d["choice"]]


def classify_agent(intent: str) -> str:
    t = intent.lower()
    if any(w in t for w in ("research", "analyze", "investigate", "compare", "whether")):
        return "researcher"
    if any(w in t for w in ("design", "wireframe", "palette", "ux", "ui")):
        return "designer"
    if any(w in t for w in ("deploy", "ship", "release", "publish")):
        return "deployer"
    if any(w in t for w in ("bug", "fix", "error", "debug", "broken", "500")):
        return "debugger"
    if any(w in t for w in ("review", "audit", "check")):
        return "reviewer"
    return "builder"


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


def decide(state: str, questions: Dict) -> Dict:
    """questions: {name: {type: choice|noul|score, instructions?, criteria?}}"""
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if key:
        try:
            return _typesafe(state, questions, key)
        except Exception:
            pass
    answers = {}
    for name, q in questions.items():
        qtype = q.get("type", "choice")
        if qtype == "noul":
            yes = _hit(state, r"\b(yes|must|require|prod|secret|deploy)\b", 1.4) + 0.2
            no = 0.6
            answers[name] = noul(yes, no)
        else:
            criteria = q.get("criteria") or {"yes": "match", "no": "not"}
            if isinstance(criteria, list):
                criteria = {str(i): c for i, c in enumerate(criteria)}
            weights = {}
            for label, desc in criteria.items():
                weights[label] = 0.25 + lexical_overlap(state, f"{label} {desc}") * 4
            answers[name] = distribution(weights)
    return {"backend": "local", "model": "local-softmax", "answers": answers}


def _typesafe(state: str, questions: Dict, key: str) -> Dict:
    """Official TypeSafe SDK. Dicts are not a valid questions payload."""
    from typesafe_sdk import Choice, Noul, Score, TypeSafeClient  # type: ignore

    mapped = {}
    for name, q in questions.items():
        qtype = q.get("type", "choice")
        instructions = q.get("instructions") or name
        if qtype == "noul":
            mapped[name] = Noul(instructions=instructions)
        elif qtype == "score":
            criteria = q.get("criteria") or ["low", "high"]
            if isinstance(criteria, dict):
                criteria = list(criteria.values())
            mapped[name] = Score(instructions=instructions, criteria=criteria)
        else:
            criteria = q.get("criteria") or {"yes": "match", "no": "not"}
            mapped[name] = Choice(instructions=instructions, criteria=criteria)

    with TypeSafeClient(api_key=key) as client:
        result = client.system_one(state, mapped)

    answers: Dict = {}
    src = getattr(result, "answers", None) or result
    choices = getattr(result, "choices", {}) or {}
    nouls = getattr(result, "nouls", {}) or {}
    scores = getattr(result, "scores", {}) or {}

    for name, q in questions.items():
        qtype = q.get("type", "choice")
        obj = None
        if hasattr(src, "get"):
            obj = src.get(name)
        if obj is None:
            bucket = {"choice": choices, "noul": nouls, "score": scores}.get(qtype, {})
            obj = bucket.get(name) if hasattr(bucket, "get") else None
        if obj is None:
            continue
        if qtype == "noul":
            n = float(getattr(obj, "noul", 0))
            answers[name] = {"noul": n, "label": "yes" if n >= 0.5 else "no"}
        else:
            answers[name] = {
                "choice": getattr(obj, "choice", getattr(obj, "score", None)),
                "probabilities": getattr(obj, "probabilities", {}),
                "confidence": getattr(obj, "confidence", 0),
            }
    return {"backend": "jev", "model": "jev-latest", "answers": answers}
