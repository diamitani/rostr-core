"""Virtual repo + tool catalog for the Jev harness backend.

The HTTP API does not touch the host filesystem. Coding-agent sessions run
against this in-memory tree so POST /v1/sessions/{id}/turns is deterministic
and safe to call from another agent.
"""
from __future__ import annotations

import re
from typing import Dict, List

TOOLS = [
    {"name": "read_file", "snippet": "Read a text file inside the repo.", "schema": {"path": "str"}, "rw": "read", "risk": "low"},
    {"name": "write_file", "snippet": "Write a text file inside the repo.", "schema": {"path": "str", "content": "str"}, "rw": "write", "risk": "med"},
    {"name": "list_dir", "snippet": "List files in a directory.", "schema": {"path": "str"}, "rw": "read", "risk": "low"},
    {"name": "grep", "snippet": "Search file contents.", "schema": {"pattern": "str", "path": "str"}, "rw": "read", "risk": "low"},
    {"name": "git_status", "snippet": "Show git status.", "schema": {}, "rw": "read", "risk": "low"},
    {"name": "git_diff", "snippet": "Show unstaged diff.", "schema": {"path": "str"}, "rw": "read", "risk": "low"},
    {"name": "bash", "snippet": "Run a shell command.", "schema": {"command": "str"}, "rw": "write", "risk": "high"},
    {"name": "pytest", "snippet": "Run tests/.", "schema": {"path": "str"}, "rw": "read", "risk": "low"},
    {"name": "memory_query", "snippet": "Search hub / session memory.", "schema": {"query": "str"}, "rw": "read", "risk": "low"},
    {"name": "rag_search", "snippet": "Retrieve outside-world or repo knowledge.", "schema": {"query": "str"}, "rw": "read", "risk": "low"},
    {"name": "deploy_vercel", "snippet": "Ship the current tree to production.", "schema": {"project": "str"}, "rw": "write", "risk": "high"},
    {"name": "browser", "snippet": "Open a page and snapshot it.", "schema": {"url": "str"}, "rw": "read", "risk": "med"},
]

READ_ONLY = {t["name"] for t in TOOLS if t["rw"] == "read" and t["risk"] == "low"}


def sensitivity(path: str) -> str:
    p = (path or "").lower()
    if re.search(r"(^|/)\.env|id_rsa|credentials|secret|\.pem|/\.ssh/", p):
        return "restricted"
    if "infra" in p or "terraform" in p:
        return "custom"
    if p.endswith((".md", ".txt", ".svg")) or p.startswith("docs/"):
        return "open"
    return "standard"

DEFAULT_FILES: Dict[str, str] = {
    "src/chat/stream.ts": """export async function streamChat(req: Request) {
  const body = await req.json()
  // BUG: treats an object as JSON text — throws 500 on every request
  const parsed = JSON.parse(body as unknown as string)
  return new Response(JSON.stringify(parsed))
}
""",
    "src/lib/auth.ts": """export function requireUser(req: Request) {
  const token = req.headers.get("authorization")
  if (!token) throw new Error("unauthorized")
  return token
}
""",
    "tests/chat.test.ts": """import { streamChat } from "../src/chat/stream"
test("streams a completion", async () => {
  const res = await streamChat(new Request("http://x", { method: "POST", body: JSON.stringify({ messages: [] }) }))
  expect(res.status).toBe(200)
})
""",
    "README.md": "# chat-service\n\nStreaming chat API. See src/chat/stream.ts.\n",
    ".env.local": "OPENAI_API_KEY=sk-live-do-not-route-to-cheap-providers\nDATABASE_URL=postgres://internal\n",
    "package.json": '{ "name": "chat-service", "scripts": { "test": "vitest" } }\n',
}

GREP_NOISE = "\n".join(
    "src/chat/stream.ts:4:  const parsed = JSON.parse(body as unknown as string)"
    if i == 7
    else "src/chat/stream.ts:5:  return new Response(JSON.stringify(parsed))"
    if i == 8
    else f"node_modules/lib/vendor-{i}.js:{i * 17}: JSON.parse(payload) // unrelated"
    for i in range(40)
)

SCENARIOS = [
    {
        "id": "fix-500",
        "title": "Fix the 500",
        "goal": "Fix error 500 in chat stream handler",
        "query": "Where is the 500 coming from in the chat stream?",
        "scope": "dev",
    },
    {
        "id": "research",
        "title": "Refunds research",
        "goal": "Research whether we should build a billing refund workflow",
        "query": "What do we already know about refunds, and is this worth building?",
        "scope": "dev",
    },
    {
        "id": "ship",
        "title": "Ship production",
        "goal": "Deploy production release to Vercel and AWS",
        "query": "Ship the current tree to production.",
        "scope": "deploy",
    },
    {
        "id": "push",
        "title": "Test then push",
        "goal": "Run pytest then git push origin main",
        "query": "Run the tests and push if they pass.",
        "scope": "dev",
    },
]


def seed_extra(scenario: str) -> List[Dict]:
    if scenario == "fix-500":
        return [
            {"kind": "tool_out", "title": "grep JSON.parse", "body": GREP_NOISE},
            {
                "kind": "reasoning",
                "title": "prior reasoning",
                "body": "Yesterday we suspected auth middleware. That was a dead end. The 500 is in streamChat.",
            },
            {
                "kind": "instruction",
                "title": "AGENTS.md / style",
                "body": "TypeScript. No any. Prefer Response.json. Do not log secrets. Tests live in tests/.",
            },
        ]
    if scenario == "research":
        return [
            {
                "kind": "instruction",
                "title": "hub learning · billing",
                "body": "Stripe refunds exist via Dashboard. No in-app refund workflow. Support spends ~4h/week on manual refunds.",
            },
            {
                "kind": "tool_out",
                "title": "rag: stripe refunds",
                "body": "Stripe Refunds API: POST /v1/refunds. Requires charge or payment_intent. Idempotency keys recommended.",
            },
        ]
    if scenario == "ship":
        return [
            {
                "kind": "instruction",
                "title": "deploy runbook",
                "body": "Production is Vercel + AWS RDS. Need approval. Rollback is vercel rollback.",
            },
        ]
    return [
        {
            "kind": "tool_out",
            "title": "CI last run",
            "body": "tests/chat.test.ts failed: streamChat threw SyntaxError JSON.parse",
        },
    ]
