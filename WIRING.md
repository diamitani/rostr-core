# Wiring the Jev harness into anything

## A. Run it as-is

```bash
python3 test_harness.py
python3 demo.py
python3 server.py --port 8787
```

## B. Use ROSTR as another agent's harness backend

```python
import json, urllib.request

def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())

s = post("http://127.0.0.1:8787/v1/sessions",
         {"goal": "Fix error 500 in chat stream handler"})
t = post(f"http://127.0.0.1:8787/v1/sessions/{s['sessionId']}/turns",
         {"query": "Where is the 500 coming from?"})
# t["assembled"] is what you send the generator — not the transcript
# t["decision"]["permission"] is allow | ask | deny
# t["decision"]["route"]["target"] is frontier | subagent | cheap | background
```

Or import the loop directly:

```python
from harness import HarnessSession
from runtime import run_worker
```

## C. Point decisions at real Jev (today)

Jev is open. No waitlist.

```bash
pip install typesafe-sdk
export TYPESAFE_API_KEY=...   # https://console.typesafe.ai/keys
```

`jev.decide()` builds real `Choice` / `Noul` / `Score` objects and calls
`TypeSafeClient.system_one`. Dicts are not a valid payload.

The adapter (`pip install 'system-one-adapter[openai]'`) is comparison-only.
Do not point PAL or per-turn routing at it.

TypeSafe's own coding-agent docs: there is no `model: "jev-latest"` that turns
Claude Code / Cursor into a Jev agent. Jev sits *beside* the generator.

## D. Point generation at your models

Same as before: `AI_GATEWAY_API_KEY` + `config.json` `default_model` /
`cheap_model`. The harness picks cheap vs frontier **per turn**, including
reprocessing cost. Mixed Opus→Sonnet→Opus on a full transcript is the trap
the paper prices out; this loop never does that.

## The six questions, every turn

| Decision | Typed answer |
|---|---|
| Context | hide / short / long / full per chunk; grep is heatmap-filtered, not prefix-sliced |
| Cache | reuse / rebuild (noul) |
| Routing | frontier / sub-agent / cheap / background + $ |
| Tools | ranked choice, top-k schemas |
| Permissions | allow / ask / deny |
| Security | open / standard / restricted / custom |

## What went wrong in v1 (and is now fixed)

The original `runtime.py` rebuilt a transcript prompt, dumped every allowed
tool, and always called `default_model`. That is a while-loop around a KV
cache. Routing looked cheap and was not. Compaction was query-blind.
Sub-agents were an extension point because passing state was hard.

The harness makes state addressable. Visibility is per query. Tools are
disclosed in tiers. Routing is priced on the assembled context, not the
session. Permissions inspect command + path, not just the binary name.

## E. Deploy on Vercel (API gateway)

ROSTR already talks to **Vercel AI Gateway** for generation (`gateway.py`,
`AI_GATEWAY_API_KEY`, `https://ai-gateway.vercel.sh/v1`). The Jev harness now
picks the model **per turn**:

| Jev route | config key | default |
|---|---|---|
| frontier | `frontier_model` | `anthropic/claude-sonnet-4-6` |
| subagent / cheap | `cheap_model` | `anthropic/claude-haiku-4-5` |
| background | `background_model` | `anthropic/claude-haiku-4-5` |

The same HTTP contract is a Vercel serverless function:

```
GET  /v1/health
POST /v1/sessions
POST /v1/sessions/{id}/turns
GET  /v1/sessions/{id}
POST /v1/generate          { system, body, route }  → Vercel AI Gateway
```

`vercel.json` + `api/index.py` wrap `server.py`. Set `AI_GATEWAY_API_KEY` in
the Vercel project. Coding agents should call this origin, not a while-loop
transcript.

rostr-platform can proxy the same `/v1` contract by setting `ROSTR_HARNESS_URL`
to the Vercel deployment.
