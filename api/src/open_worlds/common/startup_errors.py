"""Turn a game server's log into the reason it failed to start.

The worker stores the excerpt on the world for its card, and stops a world
whose server hit a known fatal error rather than leaving it "starting".

* **A known fatal line** (``excerpt_from_fatal``): some failures leave the
  process alive (Forge after "Failed to load datapacks"). Each adapter lists
  these (``fatal_log_patterns``); the excerpt runs from just before the first
  one through its stack trace.
* **Otherwise** (``excerpt_from_tail``): the last lines of the log.

The admin password is redacted, since excerpts are shown to anyone with access
to the world.
"""

from __future__ import annotations

import re

#: ANSI colour and cursor codes, which would show as garbage in the UI.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
#: Lines kept above the fatal line, for context.
CONTEXT_BEFORE = 3
#: Lines kept from the fatal line onward: the line and its stack trace.
CONTEXT_AFTER = 60
#: Lines kept when there is no known fatal line.
TAIL_LINES = 40
#: Hard cap on what is stored, whatever the line counts produce.
MAX_CHARS = 8000


def clean(raw: str, redact: tuple[str, ...] = ()) -> list[str]:
    """The log as plain lines: ANSI codes stripped, secrets replaced."""
    text = _ANSI.sub("", raw).replace("\r", "")
    for secret in redact:
        if secret:
            text = text.replace(secret, "********")
    return text.split("\n")


def _cap(lines: list[str], keep_end: bool = False) -> str:
    """Join and cap at MAX_CHARS, from whichever end matters: a fatal excerpt
    leads with the fatal line, a tail's newest lines are at the bottom."""
    text = "\n".join(lines).strip("\n")
    if len(text) > MAX_CHARS:
        if keep_end:
            text = "…" + text[-(MAX_CHARS - 1) :].lstrip()
        else:
            text = text[: MAX_CHARS - 1].rstrip() + "…"
    return text


def find_fatal(lines: list[str], patterns: tuple[str, ...]) -> int | None:
    """Index of the first line containing any of ``patterns``, or None."""
    for i, line in enumerate(lines):
        if any(p in line for p in patterns):
            return i
    return None


def excerpt_from_fatal(
    raw: str, patterns: tuple[str, ...], redact: tuple[str, ...] = ()
) -> str | None:
    """The excerpt around the first known fatal line, or None if there is none."""
    lines = clean(raw, redact)
    i = find_fatal(lines, patterns)
    if i is None:
        return None
    return _cap(lines[max(0, i - CONTEXT_BEFORE) : i + CONTEXT_AFTER])


def excerpt_from_tail(raw: str, redact: tuple[str, ...] = ()) -> str:
    """The last lines of the log, for a failure with no known fatal line."""
    lines = [line for line in clean(raw, redact) if line.strip()]
    return _cap(lines[-TAIL_LINES:], keep_end=True)


def summary(excerpt: str, patterns: tuple[str, ...] = ()) -> str:
    """One line to lead with: the fatal line if there is one, else the last."""
    lines = [line for line in excerpt.split("\n") if line.strip()]
    if not lines:
        return ""
    i = find_fatal(lines, patterns)
    return (lines[i] if i is not None else lines[-1]).strip()
