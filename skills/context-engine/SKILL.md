# context-engine

> The agent's memory. A loop that runs on EVERY session, in the background:
> it saves the session as a compressed `.md`, stores it, indexes it — and
> the next session pulls the last one (or all of them) back.

**Price:** $15 · **Vertical:** developer · **Version:** 2.0.0

## Always-on, not called manually

The agent does **not** invoke this skill per session. The **runtime** runs
the loop automatically on two triggers:

1. **Length trigger** — when the context window approaches its end
   (default: 75% of the model's token limit) → distill the session so
   far → save → compress → index → continue with the distilled summary.
2. **Time trigger** — periodic checkpoint during long sessions (default:
   every 30 minutes) → plus a final save at session end.

The save/compress/index path runs in a **background thread**
(fire-and-forget). A checkpoint can never block or break the agent —
failures are logged, never raised.

Config (env):
- `CONTEXT_CHECKPOINT_TOKENS_PCT` — default `75`
- `CONTEXT_CHECKPOINT_MINUTES` — default `30`
- `CONTEXT_BACKGROUND` — default `true` (set `false` for synchronous
  checkpoints in tests/scripts)

## The procedure the runtime follows

### 1. Save

At session end (or on a trigger): the transcript becomes a markdown file.

### 2. Compress + store

The `.md` goes into the storage folder:

```
storage/sessions/<project_id>/<session_id>.md
```

The file path **is** the link — the index row points at it. Fresh
sessions stay raw (hot tier).

### 3. Index

One row goes into the **master brain library** — the single central index
of everything (sessions, ingested sources, links):

| column | meaning |
|---|---|
| `id` | row id |
| `kind` | `session` \| `source` \| `link` |
| `project_id` | knowledge never crosses projects |
| `path` | the link: storage path for sessions, URL for sources/links |
| `summary` | one-line description (keyword-searchable) |
| `bytes_raw` / `bytes_stored` | size before/after compression |
| `compression_level` | `raw` \| `gzip` \| `summary+gzip` |
| `created_at` / `last_accessed` | age drives the compression tiers |

Local backend: `storage/index.json`. Production: the `kb_items` table
in Supabase (schema in `supabase_schema.sql`).

### 4. Load (next session)

At session start the runtime pulls the **last** session item (or **all**
of them on request), decompressed on read, and injects them into the new
session's context. `load_last(project_id)` / `load_sessions(project_id,
limit)`.

## Continual compression (the library shrinks itself)

A maintenance pass (`compress_library(project_id)`) runs on a schedule
and re-tiers every session row by age:

- **Hot** (< 7 days) — stays raw `.md`. Recent work must be instantly
  readable.
- **Warm** (< 30 days) — gzipped. Full text kept, ~70–80% smaller.
- **Cold** (> 30 days) — auto-summarized (extractive: headings + opening,
  clearly labeled `AUTO-SUMMARY`), then gzipped. The gist survives; the
  bulk doesn't.

Every pass reports `{processed, gzipped, summarized, bytes_before,
bytes_after, bytes_saved}` so you can watch the library fit your data
limits.

## The two halves (don't mix them up)

- **Context Engine (this skill):** session memory — what the agent
  itself did and learned. `save_session` / `load_last` / `load_sessions`
  / `compress_library`.
- **RAG DAL (`ragdal.py`):** outside-world research — URL ingest,
  chunking, link graph, vector retrieval. Its sources and links land in
  the *same* master library as `source`/`link` rows.

## Outputs

- Checkpoint rows in the master index; decompressed session text on load.
- `pack_text` — the injectable session-memory string PAL stage 2 adds
  to every compiled prompt.

## Pairs well with

- Every skill: memory makes all of them smarter across sessions.
- `build-a-skill` — past sessions are the raw material for new skills.
