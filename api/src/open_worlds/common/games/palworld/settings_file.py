"""Read, merge and write Palworld's ``PalWorldSettings.ini``.

The file, not the database, is a world's source of truth for its rules: it
travels with every backup and import, and it carries keys we have never heard
of untouched. The image would rewrite it from env vars on every boot, so that
is disabled (``DISABLE_GENERATE_SETTINGS=true``) and the merge happens here,
before the container starts.

The format is one line, ``OptionSettings=(Key=Value,...)``. Quoted strings and
parenthesised lists can both contain commas, so splitting tracks quotes and
paren depth.
"""

from __future__ import annotations

from collections.abc import Mapping

#: The only section the game reads world rules from.
SECTION = "[/Script/Pal.PalGameWorldSettings]"
#: The single key inside it, whose value holds every setting.
OPTION_KEY = "OptionSettings"

#: Characters that cannot survive in a value. The format has no escapes, so a
#: stray quote or comma corrupts every setting after it. Stripped here; the API
#: rejects them earlier with a clearer error.
UNSAFE_VALUE_CHARS = '",()\r\n'


class SettingsError(ValueError):
    """A settings file could not be understood.

    Raised rather than returning empty: a parse failure must not reset a real
    world to defaults.
    """


def _split_top_level(body: str) -> list[str]:
    """Split on commas that are not inside quotes or parentheses."""
    items: list[str] = []
    depth = 0
    quoted = False
    start = 0
    for i, ch in enumerate(body):
        if ch == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(body[start:i])
            start = i + 1
    items.append(body[start:])
    return [item for item in items if item.strip()]


def parse(text: str) -> dict[str, str]:
    """Settings from an ini's text, as key -> raw value (quotes/parens intact).

    Kept verbatim so an unmodified file re-serialises byte for byte.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(OPTION_KEY):
            continue
        _, _, rhs = stripped.partition("=")
        rhs = rhs.strip()
        if not (rhs.startswith("(") and rhs.endswith(")")):
            raise SettingsError(f"{OPTION_KEY} is not a parenthesised list")
        out: dict[str, str] = {}
        for item in _split_top_level(rhs[1:-1]):
            key, sep, value = item.partition("=")
            if not sep:
                raise SettingsError(f"setting {item.strip()!r} has no value")
            out[key.strip()] = value
        return out
    raise SettingsError(f"no {OPTION_KEY} line found")


def serialize(settings: Mapping[str, str]) -> str:
    """The full file text for these settings, ready to write."""
    body = ",".join(f"{key}={value}" for key, value in settings.items())
    return f"{SECTION}\n{OPTION_KEY}=({body})\n"


def quote(value: str) -> str:
    """A string value, stripped of characters that would break the format."""
    cleaned = "".join(ch for ch in value if ch not in UNSAFE_VALUE_CHARS)
    return f'"{cleaned}"'


def unquote(value: str | None) -> str:
    """The text of a quoted value, for reading settings back out of a file."""
    if value is None:
        return ""
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def clamp(
    settings: Mapping[str, str], ceilings: Mapping[str, float]
) -> tuple[dict[str, str], list[str]]:
    """Lower any setting that exceeds an operator-configured ceiling.

    Returns the new settings and the names lowered, so the user can be told.
    Empty by default: several knobs worth capping ship as unlimited, so any
    ceiling is a departure from vanilla.
    """
    out = dict(settings)
    lowered: list[str] = []
    for key, ceiling in ceilings.items():
        raw = out.get(key)
        if raw is None:
            continue
        try:
            current = float(raw.strip().strip('"'))
        except ValueError:
            continue
        # 0 and -1 mean "unlimited" for these keys, so they exceed any ceiling.
        unlimited = current <= 0
        if unlimited or current > ceiling:
            out[key] = _same_shape(raw, ceiling)
            lowered.append(key)
    return out, lowered


def _same_shape(original: str, value: float) -> str:
    """Render ``value`` the way ``original`` was written (int vs float)."""
    return f"{value:.6f}" if "." in original else str(int(value))


#: Ceilings applied to imported settings; see :func:`clamp`.
DEFAULT_CEILINGS: dict[str, float] = {}


def render(
    existing: str | None,
    overrides: Mapping[str, str],
    ceilings: Mapping[str, float] | None = None,
) -> tuple[str, list[str]]:
    """The file text for a world: its own settings, with ours laid over the top.

    With no ``existing`` settings the result is just the overrides, and the game
    supplies vanilla defaults for the rest.
    """
    base: dict[str, str] = {}
    if existing and existing.strip():
        base = parse(existing)
    base, lowered = clamp(base, ceilings if ceilings is not None else DEFAULT_CEILINGS)
    base.update(overrides)
    return serialize(base), lowered
