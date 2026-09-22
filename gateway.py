"""LLM access through Vercel AI Gateway (OpenAI-compatible endpoint).

Stdlib only. Auth order:
  1. AI_GATEWAY_API_KEY  → https://ai-gateway.vercel.sh/v1
  2. VERCEL_OIDC_TOKEN   → same door (automatic on a Vercel deploy)
  3. XAI_API_KEY         → https://api.x.ai/v1  (preview fallback)

When the door is Vercel and XAI_API_KEY is also set, Grok is sent as BYOK
so frontier turns do not spend gateway credits. Fallback model lists ride
the OpenAI-compatible `models` array.

Live catalog slugs use dots (claude-sonnet-4.6), not hyphens.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


GATEWAY_BASE = "https://ai-gateway.vercel.sh/v1"
XAI_BASE = "https://api.x.ai/v1"

VERCEL_MODELS = {
    "frontier": "spacexai/grok-4.5",
    "subagent": "anthropic/claude-sonnet-4.6",
    "cheap": "google/gemini-3.1-flash-lite",
    "background": "google/gemini-2.5-flash-lite",
}

VERCEL_FALLBACKS = {
    "frontier": ["spacexai/grok-4.1-fast-reasoning", "anthropic/claude-sonnet-4.6"],
    "subagent": ["spacexai/grok-4.5", "anthropic/claude-haiku-4.5"],
    "cheap": ["spacexai/grok-4.1-fast-non-reasoning", "google/gemini-2.5-flash-lite"],
    "background": ["google/gemini-3.1-flash-lite", "spacexai/grok-4.1-fast-non-reasoning"],
}

XAI_MODELS = {k: "grok-4.5" for k in VERCEL_MODELS}

CONFIG_KEYS = {
    "frontier": "frontier_model",
    "subagent": "subagent_model",
    "cheap": "cheap_model",
    "background": "background_model",
}


class GatewayError(RuntimeError):
    pass


def _trim(key: str) -> str | None:
    v = os.environ.get(key, "").strip()
    return v or None


def vercel_secret() -> str | None:
    return _trim("AI_GATEWAY_API_KEY") or _trim("VERCEL_OIDC_TOKEN")


def vercel_auth_kind() -> str:
    if _trim("AI_GATEWAY_API_KEY"):
        return "api-key"
    if _trim("VERCEL_OIDC_TOKEN"):
        return "oidc"
    return "none"


def models_from_config(config: dict | None, defaults: dict) -> dict:
    gw = (config or {}).get("gateway", {})
    out = dict(defaults)
    for route, key in CONFIG_KEYS.items():
        if gw.get(key):
            out[route] = gw[key]
    if gw.get("default_model") and not gw.get("frontier_model"):
        out["frontier"] = gw["default_model"]
    return out


def resolve_gateway(config: dict | None = None) -> dict:
    gw = (config or {}).get("gateway", {})
    if vercel_secret():
        return {
            "provider": "vercel-ai-gateway",
            "auth": vercel_auth_kind(),
            "base_url": gw.get("base_url") or GATEWAY_BASE,
            "models": models_from_config(config, VERCEL_MODELS),
            "ready": True,
            "key": vercel_secret(),
        }
    if _trim("XAI_API_KEY"):
        return {
            "provider": "xai-direct",
            "auth": "xai-key",
            "base_url": gw.get("xai_base_url") or XAI_BASE,
            "models": dict(XAI_MODELS),
            "ready": True,
            "key": _trim("XAI_API_KEY"),
        }
    return {
        "provider": "none",
        "auth": "none",
        "base_url": gw.get("base_url") or GATEWAY_BASE,
        "models": models_from_config(config, VERCEL_MODELS),
        "ready": False,
        "key": None,
    }


def model_for(route: str, models: dict) -> str:
    return models.get(route) or models.get("frontier") or VERCEL_MODELS["frontier"]


def fallbacks_for(route: str, provider: str) -> list[str]:
    if provider != "vercel-ai-gateway":
        return []
    return list(VERCEL_FALLBACKS.get(route) or [])


def _effective_model(model: str, resolved: dict) -> str:
    if resolved["provider"] != "xai-direct":
        return model
    name = model.split("/")[-1]
    return name if name.startswith("grok") else "grok-4.5"


def public_gateway_info(config: dict | None = None) -> dict:
    resolved = resolve_gateway(config)
    on_vercel = bool(_trim("VERCEL") or _trim("VERCEL_OIDC_TOKEN"))
    if resolved["provider"] == "vercel-ai-gateway":
        note = (
            "Jev picks the route. Vercel OIDC opens the AI Gateway — no API key in the workspace."
            if resolved["auth"] == "oidc"
            else "Jev picks the route. Vercel AI Gateway buys the model, with Grok as BYOK when XAI_API_KEY is present."
        )
    elif resolved["provider"] == "xai-direct":
        note = (
            "Waiting on Vercel OIDC. Preview is on xAI grok-4.5 with the same OpenAI shape."
            if on_vercel
            else "AI Gateway is not authed here (OIDC appears on publish). Generator is xAI grok-4.5 until then."
        )
    else:
        note = "No generator configured. Decision API still runs. Set AI_GATEWAY_API_KEY, deploy on Vercel, or XAI_API_KEY."
    return {
        "provider": resolved["provider"],
        "auth": resolved["auth"],
        "baseUrl": resolved["base_url"],
        "ready": resolved["ready"],
        "models": resolved["models"],
        "fallbacks": VERCEL_FALLBACKS if resolved["provider"] == "vercel-ai-gateway" else {},
        "onVercel": on_vercel,
        "note": note,
    }


def fetch_credits() -> dict | None:
    key = vercel_secret()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            GATEWAY_BASE.rstrip("/") + "/credits",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=2.5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {
            "balance": str(data.get("balance", "0")),
            "totalUsed": str(data.get("total_used", "0")),
        }
    except Exception:
        return None


def gateway_status(config: dict | None = None) -> dict:
    info = public_gateway_info(config)
    if info["provider"] == "vercel-ai-gateway":
        info["credits"] = fetch_credits()
    else:
        info["credits"] = None
    return info


def _byok_block() -> dict | None:
    xai = _trim("XAI_API_KEY")
    if not xai:
        return None
    return {"xai": [{"apiKey": xai}]}


def complete(
    model,
    messages,
    config,
    max_tokens=1024,
    temperature=0.2,
    mock_tag=None,
    route=None,
    response_format=None,
):
    """One chat completion. `messages` is a list of {role, content} dicts."""
    gw = config["gateway"]
    rt = config["runtime"]
    if os.environ.get(rt["mock_mode_env"], "") == "1":
        return _mock_complete(mock_tag)

    resolved = resolve_gateway(config)
    model = _effective_model(model, resolved)
    if not resolved["ready"] or not resolved["key"]:
        raise GatewayError(
            "No generator key. Set AI_GATEWAY_API_KEY, deploy on Vercel for OIDC, or XAI_API_KEY."
        )

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if resolved["provider"] == "vercel-ai-gateway":
        if route:
            fallbacks = fallbacks_for(route, resolved["provider"])
            if fallbacks:
                payload["models"] = fallbacks
        byok = _byok_block()
        gateway_opts = {}
        if byok:
            gateway_opts["byok"] = byok
        if gateway_opts:
            payload["providerOptions"] = {"gateway": gateway_opts}
    if response_format is not None:
        payload["response_format"] = response_format

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        resolved["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {resolved['key']}",
            "Content-Type": "application/json",
            "http-referer": "https://rostr.diamitani.com",
            "x-title": "ROSTR Jev harness",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=gw.get("timeout_s", 120)) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GatewayError(f"{resolved['provider']} HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
    return data["choices"][0]["message"]["content"]


def embed(text, config, model="openai/text-embedding-3-small"):
    """One embedding vector via the Vercel AI Gateway (OpenAI-compatible).

    Used by the Context Engine's Supabase backend. Raises GatewayError
    when the key is missing (never silently returns a fake vector).
    """
    gw = config["gateway"]
    rt = config["runtime"]
    if os.environ.get(rt["mock_mode_env"], "") == "1":
        import hashlib
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [((b - 128) / 128.0) for b in (h * 24)[:1536]]

    resolved = resolve_gateway(config)
    if resolved["provider"] != "vercel-ai-gateway" or not resolved["key"]:
        api_key = _trim(gw.get("api_key_env", "AI_GATEWAY_API_KEY"))
        if not api_key:
            raise GatewayError(
                f"Set {gw.get('api_key_env', 'AI_GATEWAY_API_KEY')} in your environment, or run with ROSTR_MOCK=1."
            )
        base = gw.get("base_url") or GATEWAY_BASE
        key = api_key
    else:
        base = resolved["base_url"]
        key = resolved["key"]

    body = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/embeddings",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
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


def complete_for_route(route, messages, config, max_tokens=1024, temperature=0.2, mock_tag=None,
                       response_format=None):
    """Pick the Vercel AI Gateway model from a Jev route, then complete."""
    resolved = resolve_gateway(config)
    model = model_for(route, resolved["models"])
    text = complete(
        model, messages, config,
        max_tokens=max_tokens, temperature=temperature, mock_tag=mock_tag,
        route=route, response_format=response_format,
    )
    return text, _effective_model(model, resolved)
