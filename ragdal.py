"""RAG DAL — the outside-world research half of Rostr's memory.

The paper's Dynamic Acquisition Layer: ingest URLs and text, chunk them,
extract the link graph, retrieve the best chunks for a query (keyword
locally, pgvector in Supabase), tier-weighted credibility, gap detection.

The Context Engine (context_engine.py) is the OTHER half: session memory
(save -> compress -> store -> index). Both write into the SAME master
brain library: RAG DAL writes kind='source' and kind='link' rows;
the Context Engine writes kind='session' rows.

Backends mirror the Context Engine's:
  JsonRagStore      — JSON files under ROSTR_KB_PATH (or ./storage/kb),
                      keyword retrieval. Zero keys.
  SupabaseRagStore  — kb_sources / kb_chunks / kb_links + pgvector.
                      Needs SUPABASE_URL, SUPABASE_SERVICE_KEY,
                      AI_GATEWAY_API_KEY (embeddings via the gateway).
"""
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error

CHUNK_TARGET_TOKENS = 500
FETCH_TIMEOUT_S = 30
MAX_INGEST_BYTES = 2_000_000
RETRIEVAL_MIN_SCORE = 1


class NotConfigured(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Shared helpers (moved here from the old context_engine.py — this is the
# research half; session memory lives in context_engine.py)
# ---------------------------------------------------------------------------
def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _tokens_estimate(text):
    return max(1, len(text) // 4)


def chunk_text(text, target_tokens=CHUNK_TARGET_TOKENS):
    target_chars = target_tokens * 4
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        if len(p) > target_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(p), target_chars):
                chunks.append(p[i:i + target_chars])
        elif len(current) + len(p) + 2 > target_chars:
            chunks.append(current)
            current = p
        else:
            current = (current + "\n\n" + p) if current else p
    if current:
        chunks.append(current)
    return chunks


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def extract_text(html_text):
    text = _TAG_RE.sub(" ", html_text)
    text = _TAG_STRIP_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


_LINK_RE = re.compile(
    r'<a\s[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)


def extract_links(html_text, base_url):
    links = []
    seen = set()
    for href, anchor in _LINK_RE.findall(html_text):
        url = urllib.parse.urljoin(base_url, href).split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        anchor_text = _WS_RE.sub(" ", extract_text(anchor)).strip()[:120]
        links.append({"to_url": url, "anchor_text": anchor_text})
    return links


def fetch_url(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "rostr-ragdal/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
            raw = r.read(MAX_INGEST_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} fetching {url}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"fetch failed for {url}: {e.reason}")
    if len(raw) > MAX_INGEST_BYTES:
        raw = raw[:MAX_INGEST_BYTES]
    ctype = ""
    try:
        ctype = r.headers.get("Content-Type", "")
    except Exception:
        pass
    text = raw.decode("utf-8", errors="replace")
    final_url = r.geturl()
    if "html" not in ctype.lower() and "<html" not in text[:2000].lower():
        return final_url, text, []
    return final_url, text, extract_links(text, final_url)


def score_chunk(query, chunk):
    terms = [w.lower() for w in re.findall(r"[a-z0-9]{3,}", query.lower())]
    if not terms:
        return 0
    hay = chunk.lower()
    return sum(hay.count(t) for t in terms)


def _record_source_in_library(index, project_id, url, title, tier):
    """Every ingested source also becomes a row in the master brain
    library (kind='source'). The URL is the link; no blob needed."""
    if index is None:
        return
    try:
        index.add_row({
            "kind": "source", "project_id": project_id,
            "path": url or f"inline:{title[:60]}",
            "summary": f"[tier {tier}] {title}",
            "bytes_raw": 0, "bytes_stored": 0,
            "compression_level": "raw",
        })
    except Exception:
        pass  # library indexing never breaks an ingest


def _record_links_in_library(index, project_id, links):
    if index is None:
        return
    try:
        for l in links:
            index.add_row({
                "kind": "link", "project_id": project_id,
                "path": l["to_url"],
                "summary": l.get("anchor_text", "")[:200],
                "bytes_raw": 0, "bytes_stored": 0,
                "compression_level": "raw",
            })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
class RagStore:
    def ingest_source(self, source, project_id, tier=3, title="",
                      index=None):
        """source: URL or raw text. Returns source_id. Also writes
        kind='source'/'link' rows into the master brain library."""
        raise NotImplementedError

    def retrieve(self, query, project_id, top_k=5):
        """Returns [{text, source_id, url, tier, score}]."""
        raise NotImplementedError

    def get_links(self, source_id):
        raise NotImplementedError

    def list_sources(self, project_id):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# JSON backend — zero keys
# ---------------------------------------------------------------------------
class JsonRagStore(RagStore):
    def __init__(self, root=None):
        self.root = root or os.environ.get(
            "ROSTR_KB_PATH",
            os.path.join(os.environ.get("ROSTR_STORAGE_PATH", "./storage"),
                         "kb"))
        os.makedirs(self.root, exist_ok=True)

    def _pdir(self, project_id):
        p = os.path.join(self.root, project_id)
        os.makedirs(p, exist_ok=True)
        return p

    @staticmethod
    def _read_json_list(path):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    @staticmethod
    def _write_json_list(path, items):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)

    @staticmethod
    def _append_jsonl(path, record):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    @staticmethod
    def _read_jsonl(path):
        out = []
        if not os.path.exists(path):
            return out
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def ingest_source(self, source, project_id, tier=3, title="",
                      index=None):
        links = []
        if isinstance(source, str) and re.match(r"https?://", source.strip()):
            final_url, html_text, links = fetch_url(source.strip())
            text = extract_text(html_text)
            url = final_url
        else:
            text = str(source)
            url = ""
        if not text.strip():
            raise RuntimeError("ingest_source: nothing to store (empty text)")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

        pdir = self._pdir(project_id)
        sources_p = os.path.join(pdir, "sources.json")
        sources = self._read_json_list(sources_p)
        source_id = f"src_{content_hash}"
        if not any(s["id"] == source_id for s in sources):
            sources.append({
                "id": source_id, "url": url,
                "title": title or (url[:120] if url else "pasted text"),
                "tier": tier, "fetched_at": _now(), "hash": content_hash,
            })
            self._write_json_list(sources_p, sources)
            for i, ch in enumerate(chunk_text(text)):
                self._append_jsonl(os.path.join(pdir, "chunks.jsonl"), {
                    "id": f"{source_id}_c{i}", "source_id": source_id,
                    "project_id": project_id, "chunk_index": i, "text": ch,
                    "token_count": _tokens_estimate(ch),
                })
            for l in links:
                self._append_jsonl(os.path.join(pdir, "links.jsonl"), {
                    "project_id": project_id, "from_source_id": source_id,
                    "to_url": l["to_url"], "anchor_text": l["anchor_text"],
                    "discovered_at": _now(),
                })
            _record_source_in_library(
                index, project_id, url,
                title or (url[:120] if url else "pasted text"), tier)
            _record_links_in_library(index, project_id, links)
        return source_id

    def retrieve(self, query, project_id, top_k=5):
        pdir = self._pdir(project_id)
        sources = {s["id"]: s
                   for s in self._read_json_list(
                       os.path.join(pdir, "sources.json"))}
        scored = []
        for ch in self._read_jsonl(os.path.join(pdir, "chunks.jsonl")):
            s = score_chunk(query, ch["text"])
            if s >= RETRIEVAL_MIN_SCORE:
                src = sources.get(ch["source_id"], {})
                scored.append((s, {
                    "text": ch["text"], "source_id": ch["source_id"],
                    "url": src.get("url", ""), "tier": src.get("tier", 3),
                    "score": s,
                }))
        scored.sort(key=lambda x: (x[1]["tier"], -x[0]))
        return [c for _, c in scored[:top_k]]

    def get_links(self, source_id):
        out = []
        for pid in os.listdir(self.root):
            p = os.path.join(self.root, pid, "links.jsonl")
            for l in self._read_jsonl(p):
                if l["from_source_id"] == source_id:
                    out.append({"to_url": l["to_url"],
                                "anchor_text": l["anchor_text"]})
        return out

    def list_sources(self, project_id):
        return self._read_json_list(
            os.path.join(self._pdir(project_id), "sources.json"))


# ---------------------------------------------------------------------------
# Supabase + pgvector backend
# ---------------------------------------------------------------------------
class SupabaseRagStore(RagStore):
    EMBED_MODEL = "openai/text-embedding-3-small"

    def __init__(self, url="", key="", gateway_config=None):
        if not url or not key:
            raise RuntimeError(
                "SupabaseRagStore needs SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        try:
            from supabase import create_client
        except ImportError:
            raise RuntimeError("pip install supabase — package not installed.")
        self.client = create_client(url, key)
        self.gateway_config = gateway_config

    def _embed(self, text):
        from gateway import embed as gateway_embed
        return gateway_embed(text, self.gateway_config,
                             model=self.EMBED_MODEL)

    def ingest_source(self, source, project_id, tier=3, title="",
                      index=None):
        links = []
        if isinstance(source, str) and re.match(r"https?://", source.strip()):
            final_url, html_text, links = fetch_url(source.strip())
            text = extract_text(html_text)
            url = final_url
        else:
            text = str(source)
            url = ""
        if not text.strip():
            raise RuntimeError("ingest_source: nothing to store (empty text)")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

        existing = self.client.table("kb_sources").select("id") \
            .eq("project_id", project_id).eq("hash", content_hash).execute()
        if existing.data:
            return existing.data[0]["id"]
        src = self.client.table("kb_sources").insert({
            "project_id": project_id, "url": url or None,
            "title": title or (url[:120] if url else "pasted text"),
            "tier": tier, "hash": content_hash,
        }).execute().data[0]
        source_id = src["id"]
        for i, ch in enumerate(chunk_text(text)):
            self.client.table("kb_chunks").insert({
                "source_id": source_id, "project_id": project_id,
                "chunk_index": i, "text": ch,
                "token_count": _tokens_estimate(ch),
                "embedding": self._embed(ch),
            }).execute()
        for l in links:
            self.client.table("kb_links").insert({
                "project_id": project_id, "from_source_id": source_id,
                "to_url": l["to_url"], "anchor_text": l["anchor_text"] or None,
            }).execute()
        _record_source_in_library(
            index, project_id, url,
            title or (url[:120] if url else "pasted text"), tier)
        _record_links_in_library(index, project_id, links)
        return source_id

    def retrieve(self, query, project_id, top_k=5):
        qvec = self._embed(query)
        res = self.client.rpc("kb_match_chunks", {
            "p_project_id": project_id,
            "p_query_embedding": qvec,
            "p_top_k": top_k,
        }).execute()
        return [{
            "text": row["text"], "source_id": row["source_id"],
            "url": row.get("url") or "", "tier": row.get("tier", 3),
            "score": round(float(row.get("similarity", 0)), 4),
        } for row in (res.data or [])]

    def get_links(self, source_id):
        res = self.client.table("kb_links") \
            .select("to_url,anchor_text").eq("from_source_id", source_id) \
            .execute()
        return [{"to_url": r["to_url"], "anchor_text": r["anchor_text"] or ""}
                for r in (res.data or [])]

    def list_sources(self, project_id):
        res = self.client.table("kb_sources") \
            .select("id,url,title,tier,fetched_at") \
            .eq("project_id", project_id) \
            .order("fetched_at", desc=True).execute()
        return res.data or []


def rag_store_from_env(gateway_config=None, index=None):
    """Supabase wins when configured; else JSON; the brain index is passed
    through so ingests also land in the master library."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY",
                         os.environ.get("SUPABASE_ANON_KEY", ""))
    store = None
    if url and key:
        try:
            store = SupabaseRagStore(url, key, gateway_config)
        except RuntimeError:
            store = None
    if store is None:
        store = JsonRagStore()
    store._library_index = index  # carried into ingest_source calls
    return store


def search(query, namespace="project", mode="general"):
    """The paper's contract, now with a real implementation behind it.
    Single-pass retrieval over the project's ingested sources; confidence
    is an honest heuristic from the top score (keyword locally, cosine on
    Supabase). Multi-pass + gap-triggered re-search remain a next step."""
    from context_engine import brain_index_from_env
    index = brain_index_from_env()
    store = rag_store_from_env(index=index)
    chunks = store.retrieve(query, namespace, top_k=5)
    if not chunks:
        return {"answer": "No stored knowledge matched this query. "
                          "Ingest sources first, then ask again.",
                "sources": [], "confidence": 0.0, "cached": True}
    top = chunks[0]
    confidence = round(min(0.95, 0.45 + 0.1 * top["score"]), 2) \
        if isinstance(top["score"], (int, float)) else 0.6
    lines = [f"Best matches for: {query}", ""]
    for c in chunks:
        src = c.get("url") or c["source_id"]
        lines.append(f"--- [tier {c.get('tier', 3)}] {src} ---")
        lines.append(c["text"][:800])
    return {
        "answer": "\n".join(lines),
        "sources": [{"url": c.get("url", ""), "tier": c.get("tier", 3),
                     "credibility": c.get("score", 0)} for c in chunks],
        "confidence": confidence,
        "cached": True,
    }
