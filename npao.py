"""NPAO — Necessity, Priority, Anxiety, Opportunity.

Classifies every task BEFORE execution, then orders the queue N -> A -> P -> O.
Anxiety runs before Priority on purpose: unresolved friction degrades the
quality of mission work.

v1 is rule-based (fast, free, predictable). The upgrade path is an LLM
classifier for ambiguous cases — same function signature, swap the inside.
"""
NECESSITY = "NECESSITY"      # I MUST — hard blocker, nothing proceeds without it
ANXIETY = "ANXIETY"          # I WON'T HAVE PEACE — open loops, friction, debt
PRIORITY = "PRIORITY"        # I NEED — mission-critical work
OPPORTUNITY = "OPPORTUNITY"  # I CAN — optional growth work

_RANK = {NECESSITY: 0, ANXIETY: 1, PRIORITY: 2, OPPORTUNITY: 3}

_BLOCKER_WORDS = ("must first", "blocked by", "depends on", "requires",
                  "before we can", "prerequisite", "unblock")
_FRICTION_WORDS = ("fix", "bug", "error", "broken", "debt", "backlog",
                   "flaky", "failing", "cleanup", "broken link")
_OPPORTUNITY_WORDS = ("nice to have", "stretch", "if time", "someday",
                      "explore", "could also", "eventually")


def classify(text, blocks=()):
    """Return (class, reason). `blocks` = list of task texts this one unblocks."""
    t = text.lower()
    if blocks or any(w in t for w in _BLOCKER_WORDS):
        return NECESSITY, "blocks downstream work — resolve first"
    if any(w in t for w in _FRICTION_WORDS):
        return ANXIETY, "unresolved friction degrades everything else"
    if any(w in t for w in _OPPORTUNITY_WORDS):
        return OPPORTUNITY, "optional growth — only with spare capacity"
    return PRIORITY, "mission work (default class)"


def order(tasks):
    """tasks: list of dicts with at least 'text' and 'npao'. Stable N->A->P->O."""
    return sorted(tasks, key=lambda d: _RANK[d["npao"]])


# ---------------------------------------------------------------------------
# EXTENSION POINT: LLM classifier
# def classify_llm(text, mission, gateway, config):
#     ask the cheap model for NECESSITY|ANXIETY|PRIORITY|OPPORTUNITY + reason,
#     fall back to classify() on parse failure. Same return shape.
# ---------------------------------------------------------------------------
