-- Rostr Context Engine + RAG DAL schema — run once in the Supabase SQL editor.
--
-- Four tables:
--   kb_items   — the MASTER BRAIN LIBRARY: one row per session, source, link
--   kb_sources — one row per ingested URL / pasted text (the provenance log)
--   kb_chunks  — ~500-token chunks with pgvector embeddings (what retrieval reads)
--   kb_links   — the link graph: outbound links discovered during ingest
--
-- Source tiers: 1 = official docs / APIs, 2 = reputable press / established
-- publications, 3 = everything else. Retrieval prefers lower tiers on ties.

-- pgvector for the embedding column
create extension if not exists vector;

-- ---------------------------------------------------------------------------
create table if not exists kb_sources (
  id          uuid primary key default gen_random_uuid(),
  project_id  text not null,
  url         text,
  title       text not null default 'untitled',
  tier        smallint not null default 3 check (tier between 1 and 3),
  fetched_at  timestamptz not null default now(),
  hash        text not null
);

create table if not exists kb_chunks (
  id          uuid primary key default gen_random_uuid(),
  source_id   uuid not null references kb_sources(id) on delete cascade,
  project_id  text not null,
  chunk_index integer not null,
  text        text not null,
  embedding   vector(1536),
  token_count integer not null default 0
);

create table if not exists kb_links (
  id             uuid primary key default gen_random_uuid(),
  project_id     text not null,
  from_source_id uuid not null references kb_sources(id) on delete cascade,
  to_url         text not null,
  anchor_text    text,
  discovered_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Indexes: project scoping first (every query is per-project), then vector.
create index if not exists idx_kb_sources_project on kb_sources (project_id);
create index if not exists idx_kb_sources_hash on kb_sources (project_id, hash);
create index if not exists idx_kb_chunks_project on kb_chunks (project_id);
create index if not exists idx_kb_chunks_source on kb_chunks (source_id);
create index if not exists idx_kb_links_project on kb_links (project_id);
create index if not exists idx_kb_links_from on kb_links (from_source_id);

-- HNSW index for cosine-similarity search on chunk embeddings.
-- (IVFFlat is the alternative; HNSW needs no training step.)
create index if not exists idx_kb_chunks_embedding
  on kb_chunks using hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Vector search RPC used by SupabaseKnowledgeStore.retrieve().
-- Returns the top-k chunks for a project with their source url + tier.
create or replace function kb_match_chunks(
  p_project_id text,
  p_query_embedding vector(1536),
  p_top_k integer default 5
)
returns table (
  text       text,
  source_id  uuid,
  url        text,
  tier       smallint,
  similarity float
)
language sql stable as $$
  select c.text, c.source_id, s.url, s.tier,
         1 - (c.embedding <=> p_query_embedding) as similarity
    from kb_chunks c
    join kb_sources s on s.id = c.source_id
   where c.project_id = p_project_id
     and c.embedding is not null
   order by c.embedding <=> p_query_embedding
   limit p_top_k;
$$;

-- ---------------------------------------------------------------------------
-- RLS: the Context Engine talks to Supabase with the SERVICE ROLE key, which
-- bypasses RLS entirely. These policies exist so that if you ever switch to
-- the anon key, reads stay scoped to... nothing (deny by default) until you
-- add your own per-user policy. Service role = full access, always.
alter table kb_sources enable row level security;
alter table kb_chunks  enable row level security;
alter table kb_links   enable row level security;

-- No permissive policies = deny-by-default for anon/authenticated roles.
-- Service role bypasses RLS, so the engine keeps working.

-- ---------------------------------------------------------------------------
-- kb_items — the MASTER BRAIN LIBRARY index. One row per item of any kind:
-- sessions (the Context Engine's session memory), sources and links (RAG
-- DAL's research). The "path" column IS the link: a storage/ file path for
-- sessions, a URL for sources/links.
-- kind: session | source | link
-- compression_level: raw | gzip | summary+gzip
-- ---------------------------------------------------------------------------
create table if not exists kb_items (
  id                uuid primary key default gen_random_uuid(),
  kind              text not null check (kind in ('session','source','link')),
  project_id        text not null,
  path              text not null,
  summary           text not null default '',
  bytes_raw         integer not null default 0,
  bytes_stored      integer not null default 0,
  compression_level text not null default 'raw'
                    check (compression_level in ('raw','gzip','summary+gzip')),
  created_at        timestamptz not null default now(),
  last_accessed     timestamptz not null default now()
);

create index if not exists idx_kb_items_project on kb_items (project_id);
create index if not exists idx_kb_items_kind on kb_items (project_id, kind);
create index if not exists idx_kb_items_created on kb_items (project_id, created_at desc);

alter table kb_items enable row level security;
-- No permissive policies = deny-by-default for anon/authenticated roles.
-- Service role bypasses RLS, so the engine keeps working.

-- Storage bucket for session blobs (SupabaseBrainIndex). Create once:
--   insert into storage.buckets (id, name, public) values ('rostr-brain','rostr-brain', false)
-- (the Python client also tries to create it automatically on first use).
