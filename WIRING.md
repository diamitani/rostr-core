# Wiring rostr-core into anything — the process

Five paths, in order of effort. Pick one; they compose.

## A. Run it as-is (2 minutes)

```bash
cd rostr-core
python3 demo.py            # mock — watch the whole loop print out
cat .rostr/state.json      # every step, decision, and task the run recorded
```

This is the fastest way to understand the system: the demo prints PAL
compiling, NPAO ordering, and each worker's thought → action → result loop.

## B. Use the runtime inside any harness (15 minutes)

The runtime is just functions. Any Python harness — Claude Code scripts,
your own app, a cron job — can drive it:

```python
import json, sys
sys.path.insert(0, "rostr-core")
from hub import Hub
from tools import default_registry
from runtime import run_master, run_worker
import pal as pal_mod

config = json.load(open("rostr-core/config.json"))
hub, tools = Hub(".rostr"), default_registry(config)

# Whole objective, orchestrated:
run_master("Research three playlist curators and draft outreach", hub, tools, config)

# Or one compiled worker, à la carte:
manifest, _ = pal_mod.compile("Write a 150-word artist bio for Copperline",
                              skill_path="rostr-core/skills/create-an-epk/SKILL.md",
                              hub=hub, tools_policy=config["tools"])
run_worker(manifest, hub, tools, config, hub.create_run("bio"))
```

Manifests are plain dicts — any LLM loop in any language can consume them.
`pal.compile()` is the integration seam: intent in, structured work order out.

## C. Point it at your models (5 minutes)

1. Vercel dashboard → AI Gateway → create API key.
2. `export AI_GATEWAY_API_KEY=...`
3. Edit `config.json`: change `default_model` / `cheap_model` to any
   `"provider/model"` string. Per-agent models go in the manifest's
   `runtime.model` — that's what the control panel's LLM picker writes.
4. `python3 demo.py --live`

No Docker, no servers. The gateway handles routing, fallbacks, and cost
tracking.

## D. Connect Supabase (30 minutes)

`hub.py` is file-backed so it runs anywhere. To make it persistent and
shared, subclass it (extension point is marked in the file) with this schema:

```sql
create table agents (id text primary key, spec jsonb,
                     created_at timestamptz default now());
create table runs (id text primary key, objective text,
                   created_at timestamptz default now());
create table steps (id bigserial primary key, run_id text references runs(id),
                    agent text, thought text, action text, args jsonb,
                    result jsonb, created_at timestamptz default now());
create table reference (id bigserial primary key, kind text, text text,
                        tags text[], embedding vector(1536),
                        created_at timestamptz default now());
```

The runtime only calls the hub's public methods, so the swap is invisible
to it. Add the `embedding` column when you implement vector search in
`search_reference()`.

## E. Add real integrations (Composio) — 20 minutes

In `tools.py`, fill in the marked `attach_composio` extension point. Your
agents then get Gmail, Calendar, Stripe, etc. through the same
`registry.call()` path — allow/deny policy still applies. Your proprietary
music tools (contract templates, EPK builder, royalty checks) stay as your
own registered functions or your own hosted MCP servers.

## What goes where as you grow

| Next step | File to change | What to do |
|---|---|---|
| Smarter prompts | `pal.py` → `_enhance()` | Add one cheap-LLM enhancement call |
| Smarter triage | `npao.py` → `classify_llm()` | Add LLM classifier, keep rules as fallback |
| Real research | `ragdal.py` → `search()` | Implement the paper §5 contract |
| Parallel workers | `runtime.py` → step 3 | ThreadPoolExecutor; keep NECESSITY blocking |
| Dashboard | new `dashboard/` | Read `.rostr/` or Supabase — the Paperclip design already maps to it |
| Ship as product | repo root | Publish skills/ as a Claude Code plugin; host the runtime as the API behind 6th Agent |
