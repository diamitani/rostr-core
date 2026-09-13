"""LLM access through Vercel AI Gateway (OpenAI-compatible endpoint).

Stdlib only — no dependencies. Models are plain "provider/model" strings,
e.g. 'anthropic/claude-sonnet-4-6'. Set AI_GATEWAY_API_KEY, or run with
ROSTR_MOCK=1 for the scripted mock used by demo.py (no keys, no spend).
"""
import json
import os
import urllib.request
import urllib.error


class GatewayError(RuntimeError):
    pass


def complete(model, messages, config, max_tokens=1024, temperature=0.2, mock_tag=None):
    """One chat completion. `messages` is a list of {role, content} dicts."""
    gw = config["gateway"]
    rt = config["runtime"]
    if os.environ.get(rt["mock_mode_env"], "") == "1":
        return _mock_complete(mock_tag)

    api_key = os.environ.get(gw["api_key_env"], "")
    if not api_key:
        raise GatewayError(
            f"Set {gw['api_key_env']} in your environment, or run with ROSTR_MOCK=1."
        )

    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        gw["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=gw.get("timeout_s", 120)) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GatewayError(f"Gateway HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
    return data["choices"][0]["message"]["content"]


def embed(text, config, model="openai/text-embedding-3-small"):
    """One embedding vector via the Vercel AI Gateway (OpenAI-compatible).

    Used by the Context Engine's Supabase backend. Raises GatewayError
    when the key is missing (never silently returns a fake vector).
    """
    gw = config["gateway"]
    rt = config["runtime"]
    if os.environ.get(rt["mock_mode_env"], "") == "1":
        # Deterministic fake vector for offline tests only.
        import hashlib
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [((b - 128) / 128.0) for b in (h * 24)[:1536]]

    api_key = os.environ.get(gw["api_key_env"], "")
    if not api_key:
        raise GatewayError(
            f"Set {gw['api_key_env']} in your environment, or run with ROSTR_MOCK=1."
        )
    body = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        gw["base_url"].rstrip("/") + "/embeddings",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=gw.get("timeout_s", 120)) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GatewayError(f"Gateway HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
    return data["data"][0]["embedding"]


# ---------------------------------------------------------------------------
# Mock mode: scripted trajectories so the whole machinery runs with no API key.
# Each mock_tag gets its own cursor; unknown tags fall back to the "worker"
# script. This is ONLY for demo.py — never used in live runs.
# ---------------------------------------------------------------------------
_MOCK_SCRIPTS = {
    "master:decompose": [
        json.dumps({"subtasks": [
            {"text": "Draft the launch checklist content for Copperline's new single"},
            {"text": "Save the checklist to demo_out/copperline-launch-checklist.md"},
        ]}),
    ],
    "worker": [
        json.dumps({"thought": "I'll write the checklist file now.",
                    "action": "write_file",
                    "args": {"path": "demo_out/copperline-launch-checklist.md",
                             "content": "# Copperline — Single Launch Checklist\n\n"
                                        "- [ ] Final master uploaded to distributor\n"
                                        "- [ ] Cover art 3000x3000 delivered\n"
                                        "- [ ] Pre-save link live\n"
                                        "- [ ] Pitch to editorial playlists (3 weeks out)\n"
                                        "- [ ] Announce on socials + mailing list\n"}}),
        json.dumps({"thought": "Checklist file written.",
                    "done": True,
                    "result": "demo_out/copperline-launch-checklist.md written"}),
    ],
}
_mock_cursors = {}


def _mock_complete(tag):
    key = tag if tag in _MOCK_SCRIPTS else "worker"
    script = _MOCK_SCRIPTS[key]
    i = _mock_cursors.get(tag or "worker", 0)
    _mock_cursors[tag or "worker"] = i + 1
    if i < len(script):
        return script[i]
    return json.dumps({"thought": "Nothing left to do.",
                       "done": True, "result": "mock trajectory complete"})
