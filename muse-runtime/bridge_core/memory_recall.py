"""Deterministic memory recall (lane 1: trigger recall, no LLM).

Pattern source: OpenClaw ``extensions/active-memory/trigger-recall.ts``
(deterministic trigger recall: word-overlap scoring, single word 0.85,
multi-word ``coverage*0.8 + min(1, overlap/2)*0.2``, blended with a
relevance term, ``matchScore >= 0.65``, top 3, context capped at 1800
chars; recall is skipped, never blocks the turn, when nothing matches).
This is our own Python implementation; it does not import OpenClaw
code. Pure functions, no LLM, no I/O.

Scoring here: the query's words are scored against each blank-line
separated section of the memory text, with the same single/multi-word
formula, blended ``trigger*0.8 + relevance*0.2`` where relevance is
``min(1, overlap/3)`` (a section matching several query words is more
relevant than one matching a single word). Sections are never cut
mid-text to fit the char cap; oversize lowest-scored sections are
dropped whole.

Fallback is the caller's job: if no snippet scores >= 0.65, the
caller should read full MEMORY.md instead of injecting a weak match.
"""
from __future__ import annotations

import re

STRONG_MATCH_SCORE = 0.65
TOP_N = 3
MAX_CONTEXT_CHARS = 1800

_WORD_RE = re.compile(r"\w+", re.UNICODE)

_RECALL_INTENT_RE = re.compile(
    r"\b(remember|recall|remind me|do you remember|did i (say|tell|mention|mean)"
    r"|what did i (say|tell|mention)|what did we (discuss|decide|agree|say)"
    r"|my preferences?|what was my|what is my preference)\b",
    re.IGNORECASE,
)


def _split_words(text: str) -> list[str]:
    """Lowercased words, length > 1 (OpenClaw's splitWords rule)."""
    words = []
    for raw in _WORD_RE.findall(text.lower()):
        word = raw.strip("_")
        if len(word) > 1:
            words.append(word)
    return words


def _score_section(query_words: list[str], section_words: set[str]) -> float:
    """Word-overlap score of one section against the query words."""
    query_unique = list(dict.fromkeys(query_words))
    if not query_unique:
        return 0.0
    overlap = sum(1 for word in query_unique if word in section_words)
    if overlap == 0:
        return 0.0
    if len(query_unique) == 1:
        trigger = 0.85
    else:
        coverage = overlap / len(query_unique)
        trigger = coverage * 0.8 + min(1.0, overlap / 2) * 0.2
    relevance = min(1.0, overlap / 3)
    return trigger * 0.8 + relevance * 0.2


def extract_snippets(memory_text: str, query: str, *, top_n: int = TOP_N,
                     max_chars: int = MAX_CONTEXT_CHARS,
                     min_score: float = STRONG_MATCH_SCORE) -> list[dict]:
    """Top matching memory sections for ``query``.

    Returns a list of ``{"text": str, "score": float}`` sorted by score
    descending (ties keep document order), at most ``top_n``, each
    scoring >= ``min_score``, total chars <= ``max_chars``. Returns []
    when nothing matches -- the caller then reads full MEMORY.md.
    """
    query_words = _split_words(query or "")
    if not query_words:
        return []
    sections = [
        block.strip()
        for block in re.split(r"\n\s*\n", (memory_text or "").strip())
    ]
    scored: list[tuple[float, int, str]] = []
    for index, section in enumerate(sections):
        if not section:
            continue
        score = _score_section(query_words, set(_split_words(section)))
        if score >= min_score:
            scored.append((round(score, 4), index, section))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = scored[:top_n] if top_n > 0 else []
    # Enforce the char cap by dropping whole lowest-scored snippets.
    while chosen and sum(len(item[2]) for item in chosen) > max_chars:
        chosen.pop()
    return [{"text": text, "score": score} for score, _, text in chosen]


def format_snippets(snippets: list[dict]) -> str:
    """Render snippets as an ``<active_memory>`` block, or "" if none."""
    if not snippets:
        return ""
    body = "\n".join(str(item["text"]) for item in snippets)
    return f"<active_memory>\n{body}\n</active_memory>"


def has_recall_intent(text: str) -> bool:
    """True when ``text`` looks like asking the agent to recall something.

    Small regex over intent words (remember/recall/what did I say/my
    preference/...); the escalation gate from OpenClaw's
    ``escalation.ts`` -- ordinary turns intentionally skip recall.
    """
    if not text:
        return False
    return bool(_RECALL_INTENT_RE.search(text))
