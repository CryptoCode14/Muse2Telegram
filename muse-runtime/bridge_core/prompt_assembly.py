"""Prompt assembly: bounded, newest-first prompt construction.

Pattern source: OpenClaw ``src/context-engine/bounded-context.ts``
(walk messages newest-first, accumulate against a budget, stop at the
limit, cut only at a message boundary) plus its
``BOUNDED_CONTEXT_NOTICE`` (``transcript-byte-limit.ts``). This is our
own Python implementation; it does not import OpenClaw code.

Chosen home: a separate module (rather than inside ``message_cache.py``)
because assembly is a pure function over already-built text sections,
independent of the cache.

Rules:
- ``static_hints`` + ``envelope`` are always kept whole, even if they
  alone exceed ``max_chars`` (cutting them would break the byte-stable
  prompt prefix the provider may cache implicitly). In that case the
  result may exceed ``max_chars`` and ``truncated`` is True.
- Optional sections are selected newest-first in priority order:
  transcript, then window, then reply chain, then memory snippets.
  A section is included whole when it fits; otherwise it is cut at a
  message boundary (one line per message in these texts), keeping the
  newest lines that fit.
- The final prompt is laid out in display order (static hints,
  envelope, memory snippets, reply chain, window, transcript).
- If anything was cut or skipped, ``BOUNDED_CONTEXT_NOTICE`` is
  prepended and ``truncated`` is True.

Pure function; lengths are characters, not tokens.
"""
from __future__ import annotations

BOUNDED_CONTEXT_NOTICE = (
    "Continuing with bounded recent context. "
    "Older history outside that window is omitted for this turn."
)


def _cut_newest(text: str, budget: int) -> str:
    """Keep the newest lines of ``text`` that fit in ``budget`` chars.

    Lines are the message boundary. Returns "" when not even the
    newest single line fits.
    """
    if budget <= 0:
        return ""
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + (1 if kept else 0)  # +1 for the joining newline
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return "\n".join(kept)


def assemble_prompt(*, static_hints: str = "", envelope: str = "",
                    reply_chain_text: str = "", window_text: str = "",
                    memory_snippets_text: str = "", transcript_text: str = "",
                    max_chars: int = 12000) -> tuple[str, bool]:
    """Assemble the turn prompt; returns ``(prompt, truncated)``."""
    fixed = [s for s in (static_hints, envelope) if s]
    # Priority order for selection (newest-first): transcript, window,
    # reply chain, memory snippets. Display order differs (below).
    optional = [
        ("transcript", transcript_text),
        ("window", window_text),
        ("reply_chain", reply_chain_text),
        ("memory", memory_snippets_text),
    ]
    display_order = ["memory", "reply_chain", "window", "transcript"]

    def joined_len(parts: list[str]) -> int:
        if not parts:
            return 0
        return sum(len(p) for p in parts) + len(parts) - 1

    parts: list[str] = list(fixed)
    selected: dict[str, str] = {}
    truncated = joined_len(parts) > max_chars

    for key, text in optional:
        if not text:
            continue
        separator = 1 if parts or selected else 0
        # Budget left for this section, accounting for the join newline
        # against the already-assembled parts plus selected sections.
        current_len = joined_len(parts) + sum(
            len(v) + 1 for v in selected.values()
        )
        remaining = max_chars - current_len - separator
        if len(text) <= remaining:
            selected[key] = text
        else:
            truncated = True
            partial = _cut_newest(text, remaining)
            if partial:
                selected[key] = partial
                # Budget is now (nearly) exhausted; lower-priority
                # sections cannot fit whole, but a very short one might
                # still fit in the leftover, so keep looping.

    ordered = list(parts)
    for key in display_order:
        if key in selected:
            ordered.append(selected[key])
    prompt = "\n".join(ordered)
    if truncated:
        prompt = f"{BOUNDED_CONTEXT_NOTICE}\n\n{prompt}" if prompt else BOUNDED_CONTEXT_NOTICE
    return prompt, truncated
