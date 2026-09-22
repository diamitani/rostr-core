"""Jev-centric coding-agent harness.

Explicit typed chunks instead of an append-only transcript. Every turn Jev
(or the local stand-in) answers: visibility, cache, route, tool, permission,
security. The runtime feeds the generator only the assembled context.

This is the TypeSafe founder's loop applied to ROSTR: the leverage is what
the harness puts in front of the model, not the while-loop itself.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

import jev

VIS = ("hide", "short", "long", "full")
ROUTES = ("frontier", "subagent", "cheap", "background")

TOOLS = [
    {"name": "read_file", "snippet": "Read a text file inside the repo.", "schema": {"path": "str"}, "rw": "read", "risk": "low"},
    {"name": "write_file", "snippet": "Write a text file inside the repo.", "schema": {"path": "str", "content": "str"}, "rw": "write", "risk": "med"},
    {"name": "list_dir", "snippet": "List files in a directory.", "schema": {"path": "str"}, "rw": "read", "risk": "low"},
    {"name": "grep", "snippet": "Search file contents.", "schema": {"pattern": "str", "path": "str"}, "rw": "read", "risk": "low"},
    {"name": "bash", "snippet": "Run a shell command.", "schema": {"command": "str"}, "rw": "write", "risk": "high"},
    {"name": "pytest", "snippet": "Run tests/.", "schema": {"path": "str"}, "rw": "read", "risk": "low"},
]

READ_ONLY = {t["name"] for t in TOOLS if t["rw"] == "read" and t["risk"] == "low"}

# Paper list prices $/MTok
OPUS_IN, OPUS_OUT = 5.0, 25.0
SONNET_IN, SONNET_OUT = 3.0, 15.0


def _sensitivity(path: str) -> str:
    p = path.lower()
    if re.search(r"(^|/)\.env|id_rsa|credentials|secret|\.pem|/\.ssh/", p):
        return "restricted"
    if "infra" in p or "terraform" in p:
        return "custom"
    if p.endswith((".md", ".txt", ".svg")) or p.startswith("docs/"):
        return "open"
    return "standard"


def _policy(sens: str) -> Dict:
    return {
        "open": {"policy": "open — cheapest first", "models": ["cheap", "subagent", "background", "frontier"]},
        "standard": {"policy": "standard — vetted providers", "models": ["subagent", "frontier"]},
        "restricted": {"policy": "restricted — first-party frontier only", "models": ["frontier"]},
        "custom": {"policy": "custom — excludes named vendors", "models": ["frontier"]},
    }[sens]


def price_routes(x_tok: int, xs_tok: int, y_tok: int = 400, z_tok: int = 200, target: str = "cheap") -> Dict:
    X, Xs, Y, Z = x_tok / 1e6, xs_tok / 1e6, y_tok / 1e6, z_tok / 1e6
    naive_frontier = OPUS_OUT * Y + OPUS_IN * Z
    naive_routed = SONNET_IN * X + SONNET_OUT * Y + SONNET_IN * Z + OPUS_IN * (Y + Z)
    if target == "frontier":
        harness = OPUS_OUT * Y + OPUS_IN * Xs
    else:
        harness = SONNET_IN * Xs + SONNET_OUT * Y + OPUS_IN * min(Xs, 0.02)
    return {
        "naiveFrontier": round(naive_frontier, 4),
        "naiveRouted": round(naive_routed, 4),
        "harness": round(harness, 4),
        "xTokens": x_tok,
        "xSmallTokens": xs_tok,
        "trap": naive_routed > naive_frontier,
    }


class HarnessSession:
    def __init__(self, goal: str, chunks: Optional[List[Dict]] = None, scope: str = "dev"):
        self.goal = goal
        self.scope = scope
        self.turn = 0
        self.chunks: List[Dict] = chunks or [{
            "id": "c0", "kind": "goal", "title": "goal", "body": goal,
            "tokens": jev.tokens(goal), "path": None, "sensitivity": "open", "rw": "read",
        }]
        self.pending_ask = None
        self._cid = 1

    @classmethod
    def from_manifest(cls, manifest: Dict) -> "HarnessSession":
        goal = manifest["instructions"]["task_description"]
        sess = cls(goal)
        allow = manifest.get("tools_enabled", {}).get("allow") or []
        if allow:
            sess.add("skill", "tool snippets",
                     "\n".join(f"{n}: see registry" for n in allow), path=None, sensitivity="open")
        return sess

    def add(self, kind, title, body, path=None, sensitivity=None, rw="read"):
        self._cid += 1
        self.chunks.append({
            "id": f"c{self._cid}",
            "kind": kind,
            "title": title,
            "body": body,
            "tokens": jev.tokens(title + body),
            "path": path,
            "sensitivity": sensitivity or (_sensitivity(path) if path else "open"),
            "rw": rw,
        })

    def record_tool(self, action, args, result):
        self.turn += 1
        body = json.dumps(result, default=str)[:4000]
        self.add("tool_out", f"{action} output", body)
        if action == "write_file" and isinstance(args, dict) and args.get("path"):
            self.add("file", args["path"], str(args.get("content", ""))[:4000],
                     path=args["path"], rw="write")

    def decide(self, query: str) -> Dict:
        vis = [self._visibility(c, query) for c in self.chunks]
        files = [c["path"] for c, v in zip(self.chunks, vis)
                 if c.get("path") and v["visibility"] != "hide"]
        ranks = {"open": 0, "standard": 1, "restricted": 2, "custom": 3}
        max_s = "open"
        for p in files:
            s = _sensitivity(p)
            if ranks[s] > ranks[max_s]:
                max_s = s
        ranked = self._rank_tools(query)
        disclosed = [r["name"] for r in ranked[:2]]
        route = self._route(query, max_s)
        perm = self._permission(query, ranked)
        x = sum(c["tokens"] for c in self.chunks)
        xs = 80
        for c, v in zip(self.chunks, vis):
            if v["visibility"] == "hide":
                continue
            xs += jev.tokens(v.get("excerpt") or "")
        return {
            "visibility": vis,
            "route": route,
            "tools": {"ranked": ranked, "disclosed": disclosed},
            "permission": perm,
            "security": {"sensitivity": max_s, "files": files[:8], "policy": _policy(max_s)["policy"]},
            "cost": price_routes(x, xs, target=route["target"]),
            "cache": jev.noul(0.4, 0.8) | {"action": "rebuild"},
        }

    def assemble(self, decision: Dict) -> str:
        lines = [
            f"You are a ROSTR worker. TASK: {self.goal}",
            f"Route: {decision['route']['target']}. Permission: {decision['permission']['action']}.",
            "",
            "TOOLS (snippets always; schemas only for selected):",
        ]
        for t in TOOLS:
            lines.append(f"- {t['name']}: {t['snippet']}")
            if t["name"] in decision["tools"]["disclosed"]:
                lines.append(f"  schema: {json.dumps(t['schema'])}")
        lines += ["", "STATE (query-aware, not a transcript):"]
        vis_by_id = {v["chunkId"]: v for v in decision["visibility"]}
        for c in self.chunks:
            v = vis_by_id.get(c["id"])
            if not v or v["visibility"] == "hide":
                continue
            body = v.get("excerpt") or c["body"]
            lines.append(f"[{v['visibility']} | {c['kind']} | {c['title']}]")
            lines.append(body)
            lines.append("")
        lines += [
            "Reply with JSON ONLY, one of:",
            '  {"thought": "...", "action": "<tool_name>", "args": {...}}',
            '  {"thought": "...", "done": true, "result": "..."}',
        ]
        return "\n".join(lines)

    def _visibility(self, c: Dict, query: str) -> Dict:
        q = f"{query} {self.goal}"
        overlap = jev.lexical_overlap(q, f"{c['title']}\n{c['body']}")
        path_hit = 3.2 if c.get("path") and c["path"].lower() in q.lower() else 0.0
        secret = c["sensitivity"] == "restricted"
        weights = {
            "hide": 0.35 + (4.5 if secret and not re.search(r"secret|env|key", q, re.I) else 0)
                    + (2.4 if c["kind"] == "tool_out" and overlap < 0.08 else 0),
            "short": 0.9 + (1.4 if c["kind"] == "goal" else 0) + overlap * 0.4
                     + (1.8 if c["kind"] == "tool_out" and c["tokens"] > 80 else 0),
            "long": 0.55 + overlap * 3.2 + (0.6 if c["kind"] == "file" else 0),
            "full": 0.15 + path_hit + (2.6 if overlap > 0.35 else 0),
        }
        if secret and not re.search(r"secret|env|key", q, re.I):
            weights["full"] = 0.02
            weights["long"] = 0.05
        d = jev.distribution(weights)
        vis = d["choice"]
        excerpt, heat = jev.excerpt_from_heat(c["body"], q, vis)
        return {"chunkId": c["id"], "visibility": vis, "confidence": d["confidence"],
                "probabilities": d["probabilities"], "excerpt": excerpt, "heat": heat}

    def _rank_tools(self, query: str) -> List[Dict]:
        text = f"{query} {self.goal}".lower()
        w = {}
        for t in TOOLS:
            score = 0.25
            if t["name"].replace("_", " ") in text or t["name"] in text:
                score += 2.4
            if t["name"] == "read_file":
                score += jev._hit(text, r"\b(read|open|where|file|handler)\b", 1.6)
            if t["name"] == "write_file":
                score += jev._hit(text, r"\b(fix|patch|write|implement)\b", 1.8)
            if t["name"] == "grep":
                score += jev._hit(text, r"\b(search|find|where|grep)\b", 2.2)
            if t["name"] == "bash":
                score += jev._hit(text, r"\b(bash|shell|git push|curl)\b", 2.4)
            if t["name"] == "pytest":
                score += jev._hit(text, r"\b(test|pytest)\b", 3.0)
            w[t["name"]] = score
        d = jev.distribution(w)
        return [{"name": k, "p": d["probabilities"][k]}
                for k, _ in sorted(d["probabilities"].items(), key=lambda kv: -kv[1])]

    def _route(self, query: str, max_s: str) -> Dict:
        t = f"{query} {self.goal}"
        w = {
            "frontier": 0.6 + jev._hit(t, r"\b(fix|bug|500|implement|write)\b", 1.8)
                        + (4 if max_s in ("restricted", "custom") else 0),
            "subagent": 0.7 + jev._hit(t, r"\b(search|grep|find|where)\b", 2.2),
            "cheap": 0.5 + jev._hit(t, r"\b(research|whether|summar|draft)\b", 2.8),
            "background": 0.35 + jev._hit(t, r"\b(eval|progress|review)\b", 3.0),
        }
        eligible = _policy(max_s)["models"]
        for k in list(w):
            if k not in eligible:
                w[k] *= 0.05
        d = jev.distribution(w)
        target = d["choice"] if d["choice"] in eligible else eligible[0]
        return {**d, "target": target, "eligible": eligible}

    def _permission(self, query: str, ranked: List[Dict]) -> Dict:
        top = ranked[0]["name"] if ranked else "read_file"
        text = query.lower()
        if re.search(r"\.env|~/\.ssh|id_rsa", text):
            return {"action": "deny", "reason": "policy exec: command touches .env or ~/.ssh", "command": query}
        if re.search(r"\b(curl|wget|nc )\b", text) and self.scope != "deploy":
            return {"action": "deny", "reason": "policy exec: network egress outside deploy scope", "command": query}
        if "git push" in text or top == "bash" and "push" in text:
            return {"action": "ask", "reason": "git push is ask, not auto", "command": query}
        if top in READ_ONLY or top in ("write_file", "pytest"):
            return {"action": "allow", "reason": "in-repo read/write / tests", "command": top}
        return {"action": "ask", "reason": "unknown high-risk command", "command": top}
