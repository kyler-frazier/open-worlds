"""The egress policy: what each game's worlds may reach, and what they may not.

The entries live with each game, in ``deploy/games/<game>/egress.py``; this
module loads them, defines what an entry is, and holds every entry to the rules
below at import.

Each game has two allow-lists, one per stage of a world's life:

* ``INSTALL`` -- a separate, short-lived container that downloads the server
  and exits (SteamCMD, a Minecraft loader's installer). It runs only the
  image's and the vendor's code: the world's own mods and plugins are hidden
  from it.
* ``PLAY`` -- the server itself, which runs the world's mods. It gets only what
  a running server needs (player authentication, crossplay), so a mod cannot
  reach the download hosts the install needed.

A name both stages need is listed in both. ``DENY`` covers both stages unless
an entry names one (``stages={"play"}``): an update check the server makes at
boot, against a host its install downloads from. A DENY entry that overlaps an
allow entry for the same game, variant and stage fails the import.

Two consumers, one list:

* The **egress guard** (this directory) beside every world enforces it for
  that world's game, server variant and stage: a name resolves only if some
  entry for them names it, and a TCP connection goes through only if an entry
  matches its name, protocol *and* port. Everything else is refused.
* The **egress exporter** (../egress-exporter) counts the guards' refusals
  that no DENY entry explains, from the dns-observer's report.

Least privilege, per entry: an entry names the games (and, for Minecraft, the
server variants) that need it, the protocol the gate must read on the
connection, the ports, and why it exists. A Minecraft world cannot reach Steam;
a Paper server cannot reach Fabric's maven; HTTPS on 8443 to an address that
also serves an approved name is refused.

Protocols, as the guard's gate reads the first bytes of a connection:

* ``tls``    -- the name in the TLS ClientHello (SNI) must match.
* ``http``   -- the Host header of every request must match.
* ``stream`` -- TCP the gate cannot read. Allowed only to an address this world
  got by looking up the entry's name. Weakest of the three; none today.

Every entry needs a ``reason``, checked at import. A refusal the guard logs
comes with a ready-made entry whose reason is ``TODO``, which fails that check
until someone writes down why the game needs it.

Names match exactly, or ``*.example.com`` matches any subdomain of
example.com (not example.com itself). Wildcards only for pools whose names
rotate -- Steam's CDN and connection servers.

DENY entries carry no protocol or ports. They are refused anyway, by the same
default-deny as anything unlisted; what they do is keep the alert quiet for a
host we refuse on purpose, on whatever port it tries next.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, replace
from pathlib import Path

# Loaded as a script's sibling module but imported back by the per-game files
# as "trust"; make sure that name is this module, not a second copy.
sys.modules.setdefault("trust", sys.modules[__name__])

PROTOCOLS = frozenset({"tls", "http", "stream"})
#: A world's stages, in order. Each game lists its entries per stage.
STAGES = ("install", "play")
HTTPS: tuple[tuple[int, int], ...] = ((443, 443),)
HTTP: tuple[tuple[int, int], ...] = ((80, 80),)


@dataclass(frozen=True)
class Entry:
    games: frozenset[str]
    name: str
    proto: str | None
    ports: tuple[tuple[int, int], ...]
    reason: str
    #: Empty means every variant of the game.
    variants: frozenset[str] = frozenset()
    #: Set by the loader from the list the entry is in. Empty means every
    #: stage: DENY entries, and ad hoc entries in tests.
    stages: frozenset[str] = frozenset()

    def applies_to(self, game: str | None, variant: str | None, stage: str | None = None) -> bool:
        if stage is not None and self.stages and stage not in self.stages:
            return False
        if game is None:  # the union of every game
            return True
        if game not in self.games:
            return False
        return not self.variants or (variant or "") in self.variants

    def name_matches(self, name: str) -> bool:
        name = (name or "").lower().rstrip(".")
        if not name:
            return False
        if self.name.startswith("*."):
            return name.endswith(self.name[1:])
        return name == self.name

    def port_matches(self, port: int) -> bool:
        return any(lo <= port <= hi for lo, hi in self.ports)


def _e(games, name, proto, ports, reason, variants=(), stages=()) -> Entry:  # type: ignore[no-untyped-def]
    """An entry. ``stages`` only on DENY, for a name one stage needs and the
    other refuses (an update check the install also downloads from)."""
    return Entry(
        frozenset(games), name, proto, tuple(ports), reason, frozenset(variants), frozenset(stages)
    )


def _games_dir() -> Path:
    """Where the per-game policies live: deploy/games/ in the repository, or
    /app/games/ beside this file in the guard and exporter images."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "games", here.parent / "games"):
        if candidate.is_dir():
            return candidate
    raise RuntimeError(f"no games/ directory beside {here} or its parent")


def _some_host(name: str) -> str:
    """A host the name covers: itself, or one under a wildcard."""
    return f"x.{name[2:]}" if name.startswith("*.") else name


def _load_game_file(path: Path) -> tuple[str, frozenset[str], list[Entry], list[Entry]]:
    """One game's ``egress.py``: its id, variants, and ALLOW and DENY entries.
    Its ``INSTALL`` and ``PLAY`` lists become ALLOW entries stamped with that
    stage."""
    spec = importlib.util.spec_from_file_location(f"egress_{path.parent.name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, "ALLOW") or not all(hasattr(module, s.upper()) for s in STAGES):
        raise ValueError(f"{path}: list what it may reach as INSTALL and PLAY, not ALLOW")
    allow: list[Entry] = []
    for stage in STAGES:
        for e in getattr(module, stage.upper()):
            if e.stages:
                raise ValueError(f"{path}: {e.name!r} takes its stage from its list")
            allow.append(replace(e, stages=frozenset({stage})))
    return module.GAME, frozenset(module.VARIANTS), allow, list(module.DENY)


def _load_games() -> tuple[dict[str, frozenset[str]], tuple[Entry, ...], tuple[Entry, ...]]:
    """Every game's ``games/<game>/egress.py``.

    A game's policy sits with the rest of the game. Each file imports this
    module for ``_e`` and the port constants, so it is loaded after them.
    """
    games: dict[str, frozenset[str]] = {}
    allow: list[Entry] = []
    deny: list[Entry] = []
    for path in sorted(_games_dir().glob("*/egress.py")):
        game, variants, game_allow, game_deny = _load_game_file(path)
        if game in games:
            raise ValueError(f"{path}: game {game} is defined twice")
        games[game] = variants
        allow += game_allow
        deny += game_deny
    if not games:
        raise RuntimeError(f"no egress policies under {_games_dir()}")
    return games, tuple(allow), tuple(deny)


#: Each game's server variants, and the ALLOW (every game's INSTALL and PLAY)
#: and DENY entries across games.
GAMES, ALLOW, DENY = _load_games()

PLACEHOLDER_REASONS = frozenset({"", "todo", "tbd", "?", "fixme"})


def validate(allow: tuple[Entry, ...] = ALLOW, deny: tuple[Entry, ...] = DENY) -> None:
    """Raise ValueError on any entry the policy cannot stand behind."""
    seen: set[tuple] = set()
    for kind, entries in (("ALLOW", allow), ("DENY", deny)):
        for e in entries:
            where = f"{kind} {e.name!r}"
            if e.reason.strip().lower() in PLACEHOLDER_REASONS:
                raise ValueError(f"{where}: every entry needs a real reason")
            if not e.games or not e.games <= GAMES.keys():
                raise ValueError(f"{where}: unknown game(s) {sorted(e.games)}")
            allowed_variants = frozenset().union(*(GAMES[g] for g in e.games))
            if not e.variants <= allowed_variants:
                raise ValueError(f"{where}: unknown variant(s) {sorted(e.variants)}")
            if e.name != e.name.strip().lower().rstrip(".") or not e.name:
                raise ValueError(f"{where}: write names lower-case, no trailing dot")
            if "*" in e.name and (
                not e.name.startswith("*.") or "*" in e.name[1:] or e.name.count(".") < 2
            ):
                raise ValueError(f"{where}: wildcards are '*.domain.tld' only")
            if all(p.isdigit() for p in e.name.lstrip("*.").split(".")) or ":" in e.name:
                raise ValueError(f"{where}: entries are names, not addresses")
            if kind == "ALLOW":
                if e.proto not in PROTOCOLS:
                    raise ValueError(f"{where}: protocol must be one of {sorted(PROTOCOLS)}")
                if not e.ports or any(not 1 <= lo <= hi <= 65535 for lo, hi in e.ports):
                    raise ValueError(f"{where}: needs ports, each range 1-65535")
                if len(e.stages) != 1 or not e.stages <= set(STAGES):
                    raise ValueError(f"{where}: belongs in exactly one of {list(STAGES)}")
            else:
                if e.proto is not None or e.ports:
                    raise ValueError(f"{where}: DENY entries carry no protocol or ports")
                if not e.stages <= set(STAGES):
                    raise ValueError(f"{where}: unknown stage(s) {sorted(e.stages)}")
                # The guard checks DENY first, so an overlap would silently
                # refuse a name an allow entry exists for.
                for a in allow:
                    if (
                        a.games & e.games
                        and (not a.variants or not e.variants or a.variants & e.variants)
                        and (not e.stages or a.stages & e.stages)
                        and (a.name_matches(_some_host(e.name)) or e.name_matches(_some_host(a.name)))
                    ):
                        raise ValueError(f"{where}: also allowed by {a.name!r} in {sorted(a.stages)}")
            for g in e.games:
                for v in e.variants or {""}:
                    for st in e.stages or {""}:
                        key = (kind, g, v, st, e.name, e.proto)
                        if key in seen:
                            raise ValueError(f"{where}: duplicate for {g} {v or 'all'} {st}")
                        seen.add(key)


validate()


class Policy:
    """The policy for one game, variant and stage; with no game, the union of
    all, and with no stage, the union of both stages.

    The guard builds one for its world's current stage, and starts from the
    union until it knows it. A stage's own denials are left out of the union:
    Forge's installer refusing sessionserver.mojang.com does not make the
    server's joins a denied name.
    """

    def __init__(
        self, game: str | None = None, variant: str | None = None, stage: str | None = None
    ) -> None:
        if game is not None and game not in GAMES:
            raise ValueError(f"unknown game {game!r}")
        if variant and (game is None or variant not in GAMES[game]):
            raise ValueError(f"unknown variant {variant!r} for {game}")
        if stage is not None and stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; one of {list(STAGES)}")
        self.game, self.variant, self.stage = game, variant or "", stage
        self.allow = tuple(e for e in ALLOW if e.applies_to(game, variant, stage))
        self.deny = tuple(
            e
            for e in DENY
            if e.applies_to(game, variant, stage) and (stage is not None or not e.stages)
        )
        self.ports: tuple[tuple[int, int], ...] = tuple(
            sorted({r for e in self.allow for r in e.ports})
        )

    def name_allowed(self, name: str) -> bool:
        """Whether any entry allows this name, on any protocol or port. The DNS
        gate's question: the connection is judged again, fully, at the gate."""
        return any(e.name_matches(name) for e in self.allow)

    def denied(self, name: str) -> Entry | None:
        return next((e for e in self.deny if e.name_matches(name)), None)

    def match(self, name: str, proto: str, port: int) -> Entry | None:
        return next(
            (
                e
                for e in self.allow
                if e.proto == proto and e.port_matches(port) and e.name_matches(name)
            ),
            None,
        )

    def stream_entries(self, port: int) -> tuple[Entry, ...]:
        return tuple(e for e in self.allow if e.proto == "stream" and e.port_matches(port))

    def port_listed(self, port: int) -> bool:
        return any(lo <= port <= hi for lo, hi in self.ports)


def explained(refusal: dict) -> bool:
    """Whether a guard's refusal record is one we meant: a DENY entry for its
    game, variant and stage names it. A record with no stage predates stages:
    only denials that hold in every stage explain it."""
    if refusal.get("reason") == "denied":
        return True
    game = refusal.get("game")
    game = game if game in GAMES else None
    variant = str(refusal.get("variant") or "")
    stage = refusal.get("stage")
    policy = Policy(
        game,
        # A Forge-only denial explains only a Forge world's refusal.
        variant if game and variant in GAMES[game] else None,
        stage=stage if stage in STAGES else None,
    )
    return policy.denied(str(refusal.get("name") or "")) is not None


def suggest(game: str, variant: str, name: str, proto: str, port: int) -> str:
    """A ready-to-paste entry for something the guard refused, for the stage's
    list (the refusal record names the stage). Its reason is a placeholder,
    which ``validate`` rejects until a real one is written."""
    variants = f", {{{variant!r}}}" if variant else ""
    return f"_e({{{game!r}}}, {name!r}, {proto!r}, (({port}, {port}),), 'TODO'{variants})"
