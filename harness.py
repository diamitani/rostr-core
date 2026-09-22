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
from catalog import DEFAULT_FILES, READ_ONLY, SCENARIOS, TOOLS, seed_extra, sensitivity as _catalog_sensitivity

VIS = ("hide", "short", "long", "full")
ROUTES = ("frontier", "subagent", "cheap", "background")

# Paper list prices $/MTok
OPUS_IN, OPUS_OUT = 5.0, 25.0
SONNET_IN, SONNET_OUT = 3.0, 15.0


def _sensitivity(path: str) -> str:
    return _catalog_sensitivity(path)


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
        "yTokens": y_tok,
        "zTokens": z_tok,
        "trap": naive_routed > naive_frontier,
    }


class HarnessSession:
    def __init__(self, goal: str, chunks: Optional[List[Dict]] = None, scope: str = "dev"):
        self.goal = goal
        self.scope = scope
        self.scenario = "fix-500"
        self.turn = 0
        self.files: Dict[str, str] = {}
        self.history: List[Dict] = []
        self.last_query = ""
        self.chunks: List[Dict] = chunks or [{
            "id": "c0", "kind": "goal", "title": "goal", "body": goal,
            "tokens": jev.tokens(goal), "path": None, "sensitivity": "open", "rw": "read",
        }]
        self.pending_ask = None
        self._cid = 1

    @classmethod
    def create(cls, goal: str, scenario: str = "fix-500", scope: Optional[str] = None) -> "HarnessSession":
        """HTTP/backend constructor: seed the virtual repo, not an empty transcript."""
        spec = next((s for s in SCENARIOS if s["id"] == scenario), SCENARIOS[0])
        sess = cls(goal, scope=scope or spec["scope"])
        sess.scenario = spec["id"]
        sess.files = dict(DEFAULT_FILES)
        for path, body in sess.files.items():
            sess.add("file", path, body, path=path, rw="read" if ".env" in path else "write")
        for extra in seed_extra(spec["id"]):
            sess.add(extra["kind"], extra["title"], extra["body"])
        return sess

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
        body = json.dumps(result, default=str)[:4000] if not isinstance(result, str) else result[:4000]
        self.add("tool_out", f"{action} output", body)
        if action == "write_file" and isinstance(args, dict) and args.get("path"):
            path = args["path"]
            content = str(args.get("content", ""))[:4000]
            self.files[path] = content
            existing = next((i for i, c in enumerate(self.chunks) if c.get("path") == path), None)
            chunk = {
                "id": f"c{self._cid + 1}",
                "kind": "file",
                "title": path,
                "body": content,
                "tokens": jev.tokens(path + content),
                "path": path,
                "sensitivity": _sensitivity(path),
                "rw": "write",
            }
            self._cid += 1
            if existing is not None:
                self.chunks[existing] = chunk
            else:
                self.chunks.append(chunk)

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
        similar = jev.lexical_overlap(query, self.last_query) if self.last_query else 0.0
        cache_noul = jev.noul(similar * 3 + (1.2 if similar > 0.4 else 0), 0.9 + self.turn * 0.05)
        cache = {**cache_noul, "action": "reuse" if cache_noul["label"] == "yes" and similar > 0.55 else "rebuild"}
        x = sum(c["tokens"] for c in self.chunks)
        xs = 80
        for c, v in zip(self.chunks, vis):
            if v["visibility"] == "hide":
                continue
            xs += jev.tokens(v.get("excerpt") or "")
        z = sum(c["tokens"] for c in self.chunks if c["kind"] == "tool_out")
        return {
            "visibility": vis,
            "route": route,
            "tools": {"ranked": ranked, "disclosed": disclosed},
            "permission": perm,
            "security": {"sensitivity": max_s, "files": files[:8], "policy": _policy(max_s)["policy"]},
            "cost": price_routes(x, xs, z_tok=max(z, 200), target=route["target"]),
            "cache": cache,
        }

    def assemble(self, decision: Dict) -> str:
        return self.assemble_pack(decision)["body"]

    def assemble_pack(self, decision: Dict) -> Dict:
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
        kept = hidden = 0
        for c in self.chunks:
            v = vis_by_id.get(c["id"])
            if not v or v["visibility"] == "hide":
                hidden += 1
                continue
            kept += 1
            body = v.get("excerpt") or c["body"]
            lines.append(f"[{v['visibility']} | {c['kind']} | {c['title']}]")
            lines.append(body)
            lines.append("")
        lines += [
            "Reply with JSON ONLY, one of:",
            '  {"thought": "...", "action": "<tool_name>", "args": {...}}',
            '  {"thought": "...", "done": true, "result": "..."}',
        ]
        body = "\n".join(lines)
        naive = "\n\n".join(f"# {c['title']}\n{c['body']}" for c in self.chunks)
        schemas = "\n".join(f"{t['name']} {json.dumps(t['schema'])} {t['snippet']}" for t in TOOLS)
        return {
            "system": "ROSTR Jev harness — assembled context, not a KV-cache transcript.",
            "body": body,
            "tokens": jev.tokens(body),
            "naiveTokens": jev.tokens(naive + schemas),
            "kept": kept,
            "hidden": hidden,
        }

    def pick_action(self, query: str, decision: Dict) -> Dict:
        perm = decision["permission"]
        if perm["action"] in ("deny", "ask"):
            return {
                "thought": ("Blocked: " if perm["action"] == "deny" else "Needs approval: ") + perm["reason"],
                "tool": perm.get("command"),
                "ok": False,
                "done": False,
                "blocked": True,
                "result": perm["reason"],
            }
        top = (decision["tools"]["ranked"] or [{"name": "read_file"}])[0]["name"]
        q = query.lower()

        if top == "grep" or re.search(r"where|coming from|find", q):
            hits = []
            for path, body in self.files.items():
                for i, line in enumerate(body.splitlines(), 1):
                    if re.search(r"JSON\.parse|500|throw", line):
                        hits.append(f"{path}:{i}: {line}")
            return {
                "thought": "Search the tree for the throw path instead of rereading the whole grep dump.",
                "tool": "grep",
                "args": {"pattern": "JSON.parse|Error 500", "path": "src"},
                "result": "\n".join(hits) or "no hits",
                "ok": True, "done": False, "blocked": False,
            }

        if top in ("rag_search", "memory_query"):
            bits = "\n".join(
                f"{c['title']}: {c['body'][:180]}"
                for c in self.chunks if c["kind"] in ("instruction", "tool_out")
            )
            return {
                "thought": "Pull hub + RAG once and share it — retrieval is the expensive part.",
                "tool": top,
                "args": {"query": query},
                "result": bits or "nothing in hub",
                "ok": True, "done": False, "blocked": False,
            }

        if top == "pytest":
            buggy = "JSON.parse(body" in self.files.get("src/chat/stream.ts", "")
            return {
                "thought": "Run tests/ before touching git.",
                "tool": "pytest",
                "args": {"path": "tests/"},
                "result": "FAILED tests/chat.test.ts — SyntaxError JSON.parse" if buggy else "10 passed",
                "ok": not buggy, "done": False, "blocked": False,
            }

        if top == "list_dir":
            return {
                "thought": "List the repo before reading.",
                "tool": "list_dir",
                "args": {"path": "."},
                "result": "\n".join(sorted(self.files)),
                "ok": True, "done": False, "blocked": False,
            }

        if top == "write_file" or re.search(r"fix|patch|implement", q):
            nxt = """export async function streamChat(req: Request) {
  const body = await req.json()
  return Response.json(body)
}
"""
            return {
                "thought": "Patch streamChat so it does not JSON.parse an object.",
                "tool": "write_file",
                "args": {"path": "src/chat/stream.ts", "content": nxt},
                "result": "wrote 4 lines to src/chat/stream.ts",
                "ok": True, "done": False, "blocked": False,
            }

        path = next((p for p in self.files if p.lower() in q), None)
        if path is None:
            path = "src/chat/stream.ts" if "stream" in q else (next(iter(self.files), None) or "README.md")
        return {
            "thought": f"Read {path} — it is the only chunk that should be full for this query.",
            "tool": "read_file",
            "args": {"path": path},
            "result": self.files.get(path, "missing"),
            "ok": True, "done": False, "blocked": False,
        }

    def apply_action(self, action: Dict) -> None:
        self.turn += 1
        if action.get("blocked") and action.get("tool"):
            self.pending_ask = {"command": action["tool"], "reason": action.get("result") or "ask"}
        else:
            self.pending_ask = None
        if action.get("tool") == "write_file" and not action.get("blocked"):
            args = action.get("args") or {}
            path, content = args.get("path"), args.get("content")
            if path and content:
                self.files[path] = content
                existing = next((i for i, c in enumerate(self.chunks) if c.get("path") == path), None)
                if existing is None:
                    self.add("file", path, content, path=path, rw="write")
                else:
                    self.chunks[existing]["body"] = content
                    self.chunks[existing]["tokens"] = jev.tokens(path + content)
        if action.get("tool") and action.get("result") and not action.get("blocked"):
            self.add("tool_out", f"{action['tool']} output", str(action["result"]))
        if action.get("thought"):
            self.add("reasoning", f"turn {self.turn} thought", action["thought"])

    def run_turn(self, query: str, approve: bool = False) -> Dict:
        if approve and self.pending_ask:
            self.pending_ask = None
        decision = self.decide(query)
        if approve and decision["permission"]["action"] == "ask":
            decision["permission"] = {**decision["permission"], "action": "allow", "reason": "human approved this turn"}
        assembled = self.assemble_pack(decision)
        action = self.pick_action(query, decision)
        self.apply_action(action)
        self.last_query = query
        record = {"n": self.turn, "query": query, "decision": decision, "assembled": assembled, "action": action}
        self.history.append(record)
        return {"decision": decision, "assembled": assembled, "action": action}

    def to_public(self, sid: str) -> Dict:
        return {
            "id": sid,
            "goal": self.goal,
            "scenario": self.scenario,
            "turn": self.turn,
            "chunks": self.chunks,
            "files": self.files,
            "history": self.history,
            "pendingAsk": self.pending_ask,
            "scope": self.scope,
        }

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
            "full": 0.15 + path_hit + (2.6 if overlap > 0.35 else 0)
                    + (2.8 if c["kind"] == "file" and re.search(r"fix|bug|500|error", q, re.I)
                       and re.search(r"stream", c["title"], re.I) else 0),
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
                score += jev._hit(text, r"\b(search|find|where|grep|coming from)\b", 2.2)
            if t["name"] == "bash":
                score += jev._hit(text, r"\b(bash|shell|git push|curl)\b", 2.4)
            if t["name"] == "pytest":
                score += jev._hit(text, r"\b(test|pytest)\b", 3.0)
            if t["name"] == "deploy_vercel":
                score += jev._hit(text, r"\b(deploy|ship|production|vercel)\b", 3.2)
            if t["name"] == "rag_search":
                score += jev._hit(text, r"\b(research|whether|know|docs)\b", 2.6)
            if t["name"] == "memory_query":
                score += jev._hit(text, r"\b(already know|hub|memory|last time)\b", 2.2)
            if t["name"] in ("git_status", "git_diff"):
                score += jev._hit(text, r"\b(git|diff|push|commit)\b", 1.8)
            if t["name"] == "browser":
                score += jev._hit(text, r"\b(page|screenshot|preview)\b", 2.0)
            if t["name"] == "list_dir":
                score += jev._hit(text, r"\b(list|tree|files)\b", 1.4)
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
            return {"action": "deny", "confidence": 0.92,
                    "reason": "policy exec: command touches .env or ~/.ssh", "command": query}
        if re.search(r"\b(curl|wget|nc )\b", text) and self.scope != "deploy":
            return {"action": "deny", "confidence": 0.88,
                    "reason": "policy exec: network egress outside deploy scope", "command": query}
        if "git push" in text or top == "deploy_vercel" or (top == "bash" and "push" in text):
            return {"action": "ask", "confidence": 0.84,
                    "reason": "git push / production deploy is ask, not auto", "command": query}
        if top in READ_ONLY or top in ("write_file", "pytest"):
            return {"action": "allow", "confidence": 0.8,
                    "reason": "in-repo read/write / tests", "command": top}
        return {"action": "ask", "confidence": 0.6, "reason": "unknown high-risk command", "command": top}
