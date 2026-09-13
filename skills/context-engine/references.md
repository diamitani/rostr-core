# Context Engine — operator references

## The trigger loop (always-on)

The runtime — not the agent — owns the loop. It watches two signals:

- **Tokens used vs. the model's limit.** At 75% (`CONTEXT_CHECKPOINT_TOKENS_PCT`)
  it checkpoints: distill what's happened so far into a compact session
  summary, save → compress → index it, then continue the run with the
  distilled summary in context. The run never loses its history; it just
  stops carrying the full weight of it.
- **Wall-clock time.** Every 30 minutes (`CONTEXT_CHECKPOINT_MINUTES`)
  during a long session, plus one final save when the session ends.

Both run in a background thread (`CONTEXT_BACKGROUND=true`). If the
checkpoint fails for any reason, the error goes to
`storage/checkpoint_errors.log` and the agent never notices.

## What "distill" means here

Distillation is the agent writing its own handoff note: decisions made,
files changed, open loops, what to do next. It is not a raw transcript
dump — a distilled session is typically 5–10% the size of the raw
session and far more useful to the next session.

## Compression tiers (why three)

| Tier | Age | Form | Rationale |
|---|---|---|---|
| hot | < 7 days | raw `.md` | Recent work gets re-read constantly; keep it instant. |
| warm | < 30 days | `.md.gz` | Full text preserved, ~70–80% smaller on disk. |
| cold | > 30 days | auto-summary `.md.gz` | The gist survives; the bulk doesn't. |

The cold-tier summarizer is extractive and honest: document opening +
headings with their first lines, capped at 4000 chars, prefixed with
`AUTO-SUMMARY`. It never invents content — it only selects.

## The master index row (the "spreadsheet")

One table, every kind of knowledge:

- `kind='session'` — written by `save_session()`. `path` = storage file.
- `kind='source'` — written by RAG DAL ingest. `path` = the URL.
- `kind='link'` — written by RAG DAL link extraction. `path` = the URL.

`bytes_raw` vs `bytes_stored` tells you exactly what compression bought
you, per row and per maintenance pass. `last_accessed` updates on every
read — a future "frequently re-read cold rows get promoted" rule can key
off it.

## Session start / session end checklist

**Start:** `load_last(project_id)` → inject the decompressed text (or
its summary for cold rows) into the opening context. Optionally
`load_sessions(project_id, limit=3)` for multi-session continuity.

**End:** `save_session(session_id, project_id, transcript_md,
summary=...)` → then `compress_library(project_id)` on a schedule
(nightly cron is fine) so tiers stay current.

## Failure modes and what to do

- **Storage full:** run `compress_library()` — cold rows shrink first.
  If still full, raise the cold threshold (age) or archive the oldest
  `summary+gzip` rows off the index (keep the row, move the blob).
- **Missing blob:** `load_*` raises; the row stays in the index so the
  gap is visible. Re-save the session or drop the row deliberately.
- **Checkpoint thread dies:** logged to `checkpoint_errors.log`; the
  agent run is unaffected. Next trigger retries.
