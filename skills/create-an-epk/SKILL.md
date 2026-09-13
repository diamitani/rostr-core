# Skill: create-an-epk

**Price:** $29 one-time
**Version:** 1.0

## Description
Build a complete electronic press kit for an independent artist: bio,
press quotes, music links, photos, tour dates, and contact — assembled
into a publishable page.

## When to use
The user wants a press kit / EPK for an artist. Trigger phrases: "make an
EPK", "press kit", "one-sheet".

## Inputs
- artist_name (required)
- genre (required)
- bio_facts: 3–10 bullet facts about the artist (required — never invent these)
- music_links: streaming URLs (optional)
- press_quotes: real quotes with attribution (optional — see guardrails)
- photo_url: press photo URL (optional)
- contact_email (optional)

## Steps
1. **Collect.** Confirm artist_name, genre, and bio_facts. If any are
   missing, ask once, then proceed with what's given.
2. **Bio.** Write three lengths from the facts: short (50 words), standard
   (150 words), long (300 words). Same facts, different depth.
3. **Press quotes.** Use ONLY supplied quotes with real attribution. If
   none were supplied, insert exactly: `[PRESS QUOTE PLACEHOLDER — add real
   quotes before publishing]`. Never invent a quote or attribute one.
4. **Assemble.** Produce one markdown document with sections: header
   (name/genre/location), bio (three lengths), music links, press quotes,
   tour dates, contact.
5. **Save.** Write to `epk/<artist-slug>.md` using the write_file tool.

## Outputs
- `epk/<artist-slug>.md` — the finished press kit document.

## Guardrails
- NEVER invent press quotes, stats, or achievements. Placeholder > fiction.
- NEVER publish or send anything — produce the file and stop. Publishing
  is a separate, human-approved step.
- Keep the artist's voice: read the bio_facts for tone before writing.
