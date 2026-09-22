# rostr-core — Jev harness runtime

Python 3, **zero dependencies** (stdlib only). The Rostr loop with the TypeSafe
founder's coding-agent blueprint applied: explicit typed state, per-turn
System One decisions, assembled context instead of an append-only transcript.

Jev is **not** the model that writes the code. It is the decision layer beside
it. Until a `TYPESAFE_API_KEY` is set, `jev.py` is a local stand-in with the
same question schema (Choice / Noul / Score).

## The pieces

| Piece | File | What it is |
|---|---|---|
| Harness | `harness.py` | Explicit chunks + 6 Jev questions: visibility, cache, route, tool, permission, security |
| System One | `jev.py` | Typed decisions. Swap for `typesafe_sdk` when you have a key |
| Runtime | `runtime.py` | Worker loop: decide → assemble → generate → tool → record chunk |
| Backend | `server.py` | Stdlib HTTP so any agent can use ROSTR as the harness |
| Orchestration | `agents/master.md` + `npao.py` | Decompose → N→A→P→O → delegate → verify |
| State | `hub.py` | Registry, runs, reference |
| Tools | `tools.py` | Allow/deny registry |
| PAL | `pal.py` | Intent → manifest. Stage 1 routing is System One |
| Gateway | `gateway.py` | Vercel AI Gateway, plus `ROSTR_MOCK=1` |

## Quickstart

```bash
python3 test_harness.py          # no network
python3 demo.py                  # mock master+workers through the harness
python3 server.py --port 8787    # HTTP backend
```

```bash
# live Jev
export TYPESAFE_API_KEY=...
# live generator
export AI_GATEWAY_API_KEY=...
python3 demo.py --live
```

## HTTP contract

```
POST /v1/sessions              {"goal":"Fix error 500 in chat stream handler","scenario":"fix-500"}
POST /v1/sessions/{id}/turns   {"query":"Where is the 500 coming from?"}
GET  /v1/sessions/{id}
POST /v1/generate              {"system","body","route"}
```

The generator (if you attach one) sees only the assembled context: hidden
chunks stay in state, tool schemas are top-k, routing is priced including
reprocessing. That is what makes cheap-model routing viable — the helper
never loads the full transcript, and the frontier never rereads it.

## What's real vs stand-in

**Real:** worker loop on assembled context, visibility ladder, costed routing,
tiered tools, programmable permissions, NPAO via System One, hub, mock demo,
HTTP backend.

**Stand-in:** local softmax engine is not Jev. Point `jev.decide()` at
`typesafe_sdk` with a TypeSafe key. RAG DAL search and Supabase hub are still
the marked upgrade paths.

See `WIRING.md`.
