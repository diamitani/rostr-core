# rostr-core v1 — a real, minimal Rostr runtime

Python 3, **zero dependencies** (stdlib only). The whole Rostr framework —
Runtime, Orchestration, State, Tools, Reference + PAL + NPAO — in ~1,200
lines you can read in one sitting.

## The five pieces → the files

| Piece | File | What it is |
|---|---|---|
| Runtime | `runtime.py` | The loop: PAL rebuilds the prompt → LLM picks a JSON action → tool runs → result recorded → repeat |
| Orchestration | `agents/master.md` + `npao.py` | Master instructions (decompose → classify → delegate → verify → log) and the N→A→P→O task classifier |
| State | `hub.py` | Registry (which agents exist), state (runs/tasks/steps), reference (learnings). JSON files in `.rostr/` |
| Tools | `tools.py` | Registry with allow/deny enforcement; built-ins: read_file, write_file, list_dir. Composio plugs in here |
| Reference | `hub.py` (`log_learning` / `search_reference`) | Append-only knowledge; PAL injects hits into every compiled prompt |
| PAL | `pal.py` | Intent → 5-stage compile → manifest dict + prompt. Rebuilt **every loop iteration** |
| Gateway | `gateway.py` | Vercel AI Gateway client (OpenAI-compatible). Mock mode for $0 testing |
| RAG DAL | `ragdal.py` | Interface only in v1 — contract defined, implementation is a marked next step |

Plus one real skill (`skills/create-an-epk/SKILL.md`), the Context Engine
(`context_engine.py` — session memory: a background loop that saves each
session as `.md`, compresses it, stores it in `storage/`, and indexes it
in the master brain library; next session pulls the last/all items back),
RAG DAL (`ragdal.py` — the outside-world research half: URL ingest,
chunking, link graph, keyword/pgvector retrieval; writes into the same
master library), and the agent instruction files (`agents/master.md`,
`agents/worker.md`).

## Context Engine quickstart

```bash
export ROSTR_STORAGE_PATH=./storage   # local brain library, zero keys
python3 - <<'EOF'
from context_engine import save_session, load_last, compress_library
save_session("sess-001", "myproj", "# Session notes\n\nDid the thing.",
             summary="Did the thing")
print(load_last("myproj")[:60])
print(compress_library("myproj"))  # hot/warm/cold tiers + bytes_saved
EOF
```

The trigger loop is always-on: `should_checkpoint(tokens_used,
max_tokens, elapsed_minutes)` fires at 75% of the context window
(`CONTEXT_CHECKPOINT_TOKENS_PCT`) or every 30 minutes
(`CONTEXT_CHECKPOINT_MINUTES`); `checkpoint(...)` saves in a background
thread (`CONTEXT_BACKGROUND=true`) so it never blocks the agent. PAL
stage 2 calls `assemble_pack()` automatically whenever a storage backend
is configured — no code changes needed. For the real database: run
`supabase_schema.sql` in the Supabase SQL editor (creates `kb_items` —
the master library index — plus `kb_sources` / `kb_chunks` / `kb_links`
for RAG DAL, enables pgvector), then set `SUPABASE_URL`,
`SUPABASE_SERVICE_KEY`, and `AI_GATEWAY_API_KEY` (RAG DAL embeddings go
through the gateway). `pip install supabase` for the Python client.

## Quickstart

```bash
cd rostr-core
python3 demo.py            # mock: full master+workers run, no key, no spend
python3 demo.py --live     # real: needs AI_GATEWAY_API_KEY in your env
```

Then read `WIRING.md` for the five ways to integrate it into anything.

## What's real vs. what's stubbed (honest list)

**Real:** the worker loop, PAL template compilation, NPAO rule-based
classification, the hub (JSON), tool allow/deny enforcement, the gateway
client, both agent instruction files, the EPK skill, the Context Engine
(JSON backend: ingest/retrieve/assemble; Supabase backend needs keys),
the mock demo.

**Stubbed / marked upgrade paths:** LLM-powered PAL enhancement (`pal.py`),
LLM NPAO fallback (`npao.py`), `ragdal.search()` (interface only — the
Context Engine is its storage layer; the search contract still needs an
implementation), Supabase adapter (`hub.py` — extension point + schema in WIRING.md), SupabaseKnowledgeStore client wiring
(`context_engine.py` — interface + schema ready, needs keys), Composio
attach (`tools.py` — extension point), parallel workers (`runtime.py` —
extension point).

## Deliberately NOT in v1

Dashboard, auth/multi-tenancy, vector search, the Chrome extension, the
marketplace. v1 is the engine. The rest bolts on later without changing
these files' public functions.
