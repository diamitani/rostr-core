"""Context Engine — session memory + the master brain library.

Patrick's design, implemented exactly:

  A skill loop runs on the session. At session end (or when context grows
  long): save the session as a .md file -> compress it -> store it in the
  storage folder (the file path IS the link) -> index it in a row.

  Next session/call: pull the LAST item or ALL items, decompressed on read.
  Compressed for space.

  The master brain library = ONE central index (the master spreadsheet) of
  everything: sessions, ingested sources, links. Continual compression
  maintenance keeps it small enough to fit data limits:
      hot  (< 7 days)  — stays raw .md
      warm (< 30 days)  — gzipped
      cold (> 30 days)  — summarized, then gzipped
  Every maintenance pass reports bytes saved.

RAG DAL (ragdal.py) is the OTHER half: outside-world research (URL ingest,
chunking, vector retrieval). It writes its sources and links into the SAME
master index (kind='source' / 'link'). The Context Engine is the
session-memory half. One database, two writers.

Backends (one interface):
  JsonBrainIndex      — storage/index.json + storage/sessions/<project>/
                        <session_id>.md(.gz). Zero keys. Works everywhere.
  SupabaseBrainIndex  — kb_items table + a Supabase Storage bucket for the
                        blobs. Needs SUPABASE_URL, SUPABASE_SERVICE_KEY
                        (service role). pip install supabase.

Env selection:
  SUPABASE_URL + SUPABASE_SERVICE_KEY  -> SupabaseBrainIndex
  ROSTR_STORAGE_PATH (default ./storage) -> JsonBrainIndex (always available)
"""
import gzip
import hashlib
import json
import os
import re
import time

# ---------------------------------------------------------------------------
# Compression tiers
# ---------------------------------------------------------------------------
HOT_DAYS = 7     # younger than this: raw .md
WARM_DAYS = 30   # younger than this: gzipped; older: summarized, then gzipped

RAW = "raw"
GZIP = "gzip"
SUMMARY_GZIP = "summary+gzip"

INDEX_FILE = "index.json"
SESSIONS_DIR = "sessions"


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _age_days(created_at):
    try:
        ts = time.mktime(time.strptime(created_at, "%Y-%m-%dT%H:%M:%S"))
        return max(0.0, (time.time() - ts) / 86400.0)
    except Exception:
        return 0.0


def _summarize(md_text, cap_chars=4000):
    """Honest extractive summary: headings + the first line under each,
    plus the opening of the document. Clearly labeled as auto-generated."""
    lines = md_text.split("\n")
    kept = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("#"):
            kept.append(ln)
            # first non-empty line after the heading
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip():
                    kept.append(nxt.strip()[:300])
                    break
    head = md_text[:1500].strip()
    body = "\n".join(kept)
    summary = (
        f"AUTO-SUMMARY (cold-tier compression; full session archived away):\n\n"
        f"{head}\n\n--- section outline ---\n{body}"
    )
    return summary[:cap_chars]


def _read_file_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Master index interface
# ---------------------------------------------------------------------------
class BrainIndex:
    """One row per item. Row shape:
      {id, kind, project_id, path, summary, bytes_raw, bytes_stored,
       compression_level, created_at, last_accessed}
    kind is one of: session | source | link. path is the link to the item:
    a storage/ file path for sessions, a URL for sources/links."""

    def add_row(self, row):
        raise NotImplementedError

    def get_row(self, row_id):
        raise NotImplementedError

    def list_rows(self, project_id, kind=None, limit=1000):
        raise NotImplementedError

    def update_row(self, row_id, fields):
        raise NotImplementedError

    def search_rows(self, project_id, query, kind=None, limit=10):
        """Keyword search over summaries. Honest: no fake vector math."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# JSON backend — zero keys
# ---------------------------------------------------------------------------
class JsonBrainIndex(BrainIndex):
    """storage/index.json (the master spreadsheet) +
    storage/sessions/<project_id>/<session_id>.md(.gz) (the blobs)."""

    def __init__(self, root=None):
        self.root = root or os.environ.get("ROSTR_STORAGE_PATH", "./storage")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(os.path.join(self.root, SESSIONS_DIR), exist_ok=True)

    # -- index file ---------------------------------------------------------
    def _index_path(self):
        return os.path.join(self.root, INDEX_FILE)

    def _read_index(self):
        p = self._index_path()
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
            except ValueError:
                return []
        return []

    def _write_index(self, rows):
        with open(self._index_path(), "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)

    # -- blob paths -----------------------------------------------------------
    def session_path(self, project_id, session_id, compressed=False):
        d = os.path.join(self.root, SESSIONS_DIR, project_id)
        os.makedirs(d, exist_ok=True)
        ext = ".md.gz" if compressed else ".md"
        return os.path.join(d, f"{session_id}{ext}")

    # -- interface --------------------------------------------------------------
    def add_row(self, row):
        rows = self._read_index()
        row = dict(row)
        row.setdefault("id", "row_" + hashlib.sha256(
            (row.get("path", "") + _now_iso()).encode()).hexdigest()[:12])
        row.setdefault("created_at", _now_iso())
        row.setdefault("last_accessed", row["created_at"])
        # Monotonic insertion order: created_at only has 1-second resolution,
        # so ties need a tiebreaker for "last item" semantics.
        row["seq"] = max([r.get("seq", 0) for r in rows] + [0]) + 1
        rows.append(row)
        self._write_index(rows)
        return row

    def get_row(self, row_id):
        for r in self._read_index():
            if r["id"] == row_id:
                return r
        return None

    def list_rows(self, project_id, kind=None, limit=1000):
        out = [r for r in self._read_index()
               if r.get("project_id") == project_id
               and (kind is None or r.get("kind") == kind)]
        # Newest first: insertion order wins, created_at breaks display ties.
        out.sort(key=lambda r: (r.get("seq", 0), r.get("created_at", "")),
                 reverse=True)
        return out[:limit]

    def update_row(self, row_id, fields):
        rows = self._read_index()
        for r in rows:
            if r["id"] == row_id:
                r.update(fields)
                self._write_index(rows)
                return r
        return None

    def search_rows(self, project_id, query, kind=None, limit=10):
        terms = [w.lower() for w in re.findall(r"[a-z0-9]{3,}", query.lower())]
        if not terms:
            return []
        scored = []
        for r in self._read_index():
            if r.get("project_id") != project_id:
                continue
            if kind is not None and r.get("kind") != kind:
                continue
            hay = (r.get("summary", "") + " " + r.get("path", "")).lower()
            score = sum(hay.count(t) for t in terms)
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:limit]]


# ---------------------------------------------------------------------------
# Supabase backend — kb_items table + Storage bucket for blobs
# ---------------------------------------------------------------------------
class SupabaseBrainIndex(BrainIndex):
    """Real database. Needs SUPABASE_URL, SUPABASE_SERVICE_KEY, and
    `pip install supabase`. Blobs live in the 'rostr-brain' Storage bucket;
    rows live in kb_items (see supabase_schema.sql)."""

    BUCKET = "rostr-brain"

    def __init__(self, url="", key=""):
        if not url or not key:
            raise RuntimeError(
                "SupabaseBrainIndex needs SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        try:
            from supabase import create_client
        except ImportError:
            raise RuntimeError("pip install supabase — package not installed.")
        self.client = create_client(url, key)
        try:
            self.client.storage.create_bucket(self.BUCKET, {"public": False})
        except Exception:
            pass  # bucket already exists

    @staticmethod
    def _to_row(d):
        return {
            "id": d["id"], "kind": d["kind"], "project_id": d["project_id"],
            "path": d["path"], "summary": d.get("summary", ""),
            "bytes_raw": d.get("bytes_raw", 0),
            "bytes_stored": d.get("bytes_stored", 0),
            "compression_level": d.get("compression_level", RAW),
            "created_at": d.get("created_at", ""),
            "last_accessed": d.get("last_accessed", ""),
        }

    def add_row(self, row):
        data = {
            "kind": row["kind"], "project_id": row["project_id"],
            "path": row["path"], "summary": row.get("summary", ""),
            "bytes_raw": row.get("bytes_raw", 0),
            "bytes_stored": row.get("bytes_stored", 0),
            "compression_level": row.get("compression_level", RAW),
        }
        res = self.client.table("kb_items").insert(data).execute()
        return self._to_row(res.data[0])

    def get_row(self, row_id):
        res = self.client.table("kb_items").select("*") \
            .eq("id", row_id).execute()
        return self._to_row(res.data[0]) if res.data else None

    def list_rows(self, project_id, kind=None, limit=1000):
        q = self.client.table("kb_items").select("*") \
            .eq("project_id", project_id).order("created_at", desc=True) \
            .limit(limit)
        if kind:
            q = q.eq("kind", kind)
        return [self._to_row(d) for d in (q.execute().data or [])]

    def update_row(self, row_id, fields):
        allowed = {"path", "summary", "bytes_raw", "bytes_stored",
                   "compression_level", "last_accessed"}
        res = self.client.table("kb_items").update(
            {k: v for k, v in fields.items() if k in allowed}) \
            .eq("id", row_id).execute()
        return self._to_row(res.data[0]) if res.data else None

    def search_rows(self, project_id, query, kind=None, limit=10):
        # Postgres-side keyword search would use to_tsvector; the honest
        # portable version filters in Python over the project's rows.
        terms = [w.lower() for w in re.findall(r"[a-z0-9]{3,}", query.lower())]
        if not terms:
            return []
        scored = []
        for r in self.list_rows(project_id, kind=kind, limit=5000):
            hay = (r["summary"] + " " + r["path"]).lower()
            score = sum(hay.count(t) for t in terms)
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:limit]]

    # -- blob helpers (Storage bucket) ----------------------------------------
    def _blob_key(self, project_id, session_id, compressed=False):
        ext = ".md.gz" if compressed else ".md"
        return f"sessions/{project_id}/{session_id}{ext}"

    def write_blob(self, project_id, session_id, data: bytes,
                   compressed=False):
        self.client.storage.from_(self.BUCKET).upload(
            self._blob_key(project_id, session_id, compressed), data,
            {"upsert": "true", "content-type":
             "application/gzip" if compressed else "text/markdown"})

    def read_blob(self, blob_path):
        # blob_path is the storage key stored in the row's "path".
        return self.client.storage.from_(self.BUCKET).download(blob_path)

    def delete_blob(self, blob_path):
        try:
            self.client.storage.from_(self.BUCKET).remove([blob_path])
        except Exception:
            pass


def brain_index_from_env():
    """Supabase wins when fully configured; otherwise the JSON backend
    (always available — storage/ under ROSTR_STORAGE_PATH or ./storage)."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if url and key:
        try:
            return SupabaseBrainIndex(url, key)
        except RuntimeError:
            pass  # supabase package missing — fall through to JSON
    return JsonBrainIndex()


# ---------------------------------------------------------------------------
# The session loop
# ---------------------------------------------------------------------------
def _read_session_text(index, row):
    """Decompress-on-read. Handles raw .md, .md.gz, and Supabase blobs."""
    path = row["path"]
    if isinstance(index, SupabaseBrainIndex):
        data = index.read_blob(path)
    else:
        data = _read_file_bytes(path)
    if path.endswith(".gz"):
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


def save_session(session_id, project_id, session_md, summary="",
                 index=None):
    """End-of-session: save .md -> store -> index a row. Returns the row."""
    index = index or brain_index_from_env()
    raw = session_md.encode("utf-8")
    if isinstance(index, SupabaseBrainIndex):
        index.write_blob(project_id, session_id, raw, compressed=False)
        path = index._blob_key(project_id, session_id, compressed=False)
    else:
        path = index.session_path(project_id, session_id, compressed=False)
        with open(path, "wb") as f:
            f.write(raw)
    return index.add_row({
        "kind": "session", "project_id": project_id, "path": path,
        "summary": summary or session_md[:500],
        "bytes_raw": len(raw), "bytes_stored": len(raw),
        "compression_level": RAW,
    })


def _touch(index, row):
    index.update_row(row["id"], {"last_accessed": _now_iso()})


def load_last(project_id, index=None):
    """Pull the LAST session item, decompressed. None if no sessions."""
    index = index or brain_index_from_env()
    rows = index.list_rows(project_id, kind="session", limit=1)
    if not rows:
        return None
    _touch(index, rows[0])
    return _read_session_text(index, rows[0])


def load_sessions(project_id, limit=10, index=None):
    """Pull the last N session items, newest first, decompressed."""
    index = index or brain_index_from_env()
    out = []
    for row in index.list_rows(project_id, kind="session", limit=limit):
        _touch(index, row)
        out.append({"row": row, "text": _read_session_text(index, row)})
    return out


def compress_library(project_id, index=None):
    """Continual-compression maintenance pass.
    hot (<7d): stays raw. warm (<30d): gzipped. cold (>30d): summarized,
    then gzipped. Returns {processed, gzipped, summarized, bytes_before,
    bytes_after, bytes_saved}."""
    index = index or brain_index_from_env()
    stats = {"processed": 0, "gzipped": 0, "summarized": 0,
             "bytes_before": 0, "bytes_after": 0, "bytes_saved": 0}
    for row in index.list_rows(project_id, kind="session", limit=10000):
        if row.get("compression_level") == SUMMARY_GZIP:
            continue  # fully compressed already
        age = _age_days(row.get("created_at", ""))
        stats["processed"] += 1
        stats["bytes_before"] += row.get("bytes_stored", 0)
        try:
            text = _read_session_text(index, row)
        except Exception:
            continue  # missing blob: leave the row, count nothing
        old_path = row["path"]
        if age < HOT_DAYS:
            stats["bytes_after"] += row.get("bytes_stored", 0)
            continue
        if age < WARM_DAYS:
            payload = gzip.compress(text.encode("utf-8"))
            level = GZIP
        else:
            payload = gzip.compress(
                _summarize(text).encode("utf-8"))
            level = SUMMARY_GZIP
            stats["summarized"] += 1
        if isinstance(index, SupabaseBrainIndex):
            session_id = old_path.split("/")[-1].split(".")[0]
            index.write_blob(project_id, session_id, payload,
                             compressed=True)
            new_path = index._blob_key(project_id, session_id,
                                       compressed=True)
            index.delete_blob(old_path)
        else:
            session_id = os.path.basename(old_path).split(".")[0]
            new_path = index.session_path(project_id, session_id,
                                          compressed=True)
            with open(new_path, "wb") as f:
                f.write(payload)
            try:
                os.remove(old_path)
            except OSError:
                pass
        index.update_row(row["id"], {
            "path": new_path, "bytes_stored": len(payload),
            "compression_level": level,
        })
        stats["gzipped"] += 1
        stats["bytes_after"] += len(payload)
    stats["bytes_saved"] = stats["bytes_before"] - stats["bytes_after"]
    return stats


# ---------------------------------------------------------------------------
# assemble_pack — what PAL stage 2 injects
# ---------------------------------------------------------------------------
def assemble_pack(intent, project_id, hub=None, index=None, last_n=2):
    """Pack = the last N sessions (decompressed) + keyword-relevant older
    sessions + relevant past decisions from the hub. Sessions are the
    agent's own memory; RAG DAL sources come from ragdal.retrieve()."""
    index = index or brain_index_from_env()
    sessions = load_sessions(project_id, limit=last_n, index=index)
    seen_ids = {s["row"]["id"] for s in sessions}
    for row in index.search_rows(project_id, intent, kind="session",
                                 limit=5):
        if row["id"] not in seen_ids:
            _touch(index, row)
            sessions.append({"row": row,
                             "text": _read_session_text(index, row)})
            seen_ids.add(row["id"])
    decisions = []
    if hub is not None:
        try:
            for e in hub.search_reference(intent):
                if e.get("kind") == "decision":
                    decisions.append({"choice": e.get("text", "")})
        except Exception:
            pass
    lines = [f"SESSION MEMORY for project '{project_id}':", ""]
    if not sessions:
        lines.append("No prior sessions stored for this project.")
    for s in sessions:
        r = s["row"]
        lvl = r.get("compression_level", RAW)
        tag = "auto-summarized" if lvl == SUMMARY_GZIP else lvl
        lines.append(f"\n--- session {r['path'].split('/')[-1]} "
                     f"({r.get('created_at', '')}, {tag}) ---")
        lines.append(s["text"][:3000])
    if decisions:
        lines.append("\nRELEVANT PAST DECISIONS:")
        for d in decisions[:5]:
            lines.append(f"  - {d['choice'][:200]}")
    return {"sessions": sessions, "decisions": decisions,
            "pack_text": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Trigger loop — the engine runs on EVERY session, in the background,
# and never blocks the agent.
#
# Two checkpoint triggers:
#   1. LENGTH-based: context window approaching its end (default 75% of the
#      model's token limit) -> distill the session so far -> save -> compress
#      -> index, then the run continues with the distilled summary.
#   2. TIME-based: periodic checkpoint during long sessions (default every
#      30 min) + a final save at session end.
#
# Config via env:
#   CONTEXT_CHECKPOINT_TOKENS_PCT (default 75)
#   CONTEXT_CHECKPOINT_MINUTES    (default 30)
#   CONTEXT_BACKGROUND           (default true)
# ---------------------------------------------------------------------------
import threading


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_bool(name, default=True):
    return os.environ.get(name, str(default)).strip().lower() not in (
        "0", "false", "no", "off")


CHECKPOINT_TOKENS_PCT = _env_float("CONTEXT_CHECKPOINT_TOKENS_PCT", 75)
CHECKPOINT_MINUTES = _env_float("CONTEXT_CHECKPOINT_MINUTES", 30)
BACKGROUND = _env_bool("CONTEXT_BACKGROUND", True)


def should_checkpoint(context_tokens, max_tokens, elapsed_minutes,
                      tokens_pct=None, minutes=None):
    """True when a checkpoint is due. Pure function — easy to unit test.

    LENGTH trigger: context_tokens / max_tokens >= tokens_pct (default 75%).
    TIME trigger:   elapsed_minutes >= minutes (default 30).
    Either one fires the checkpoint."""
    pct = tokens_pct if tokens_pct is not None else CHECKPOINT_TOKENS_PCT
    mins = minutes if minutes is not None else CHECKPOINT_MINUTES
    if max_tokens and max_tokens > 0:
        if (context_tokens / max_tokens) * 100.0 >= pct:
            return True
    if elapsed_minutes is not None and elapsed_minutes >= mins:
        return True
    return False


def _checkpoint_job(session_id, project_id, transcript_md, summary, index):
    """The actual save->compress->index work. Failures are logged, never
    raised: a checkpoint must never break a running agent."""
    try:
        row = save_session(session_id, project_id, transcript_md,
                           summary=summary, index=index)
        # Fresh checkpoints are hot-tier; run one maintenance pass so the
        # library keeps shrinking itself in the background too.
        compress_library(project_id, index=index)
        return row
    except Exception as e:  # noqa: BLE001 — fire-and-forget by design
        try:
            with open(os.path.join(
                    getattr(index, "root", "/tmp"), "checkpoint_errors.log"),
                    "a", encoding="utf-8") as f:
                f.write(f"{_now_iso()} session={session_id} error={e}\n")
        except Exception:
            pass
        return None


def checkpoint(session_id, project_id, transcript_md, summary="",
               index=None, background=None):
    """Fire-and-forget checkpoint: save -> compress -> index without
    blocking the agent. Set background=False (or CONTEXT_BACKGROUND=false)
    to run synchronously (tests, scripts). Returns the Thread, or the row
    when run synchronously."""
    bg = BACKGROUND if background is None else background
    index = index or brain_index_from_env()
    if not bg:
        return _checkpoint_job(session_id, project_id, transcript_md,
                               summary, index)
    t = threading.Thread(
        target=_checkpoint_job,
        args=(session_id, project_id, transcript_md, summary, index),
        name=f"ce-checkpoint-{session_id}", daemon=True)
    t.start()
    return t
