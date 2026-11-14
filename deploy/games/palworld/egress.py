"""Palworld's egress policy: what its worlds may reach, and what they may not.

Loaded by egress-guard/trust.py, which documents the rules every entry
follows (protocols, ports, stages, a required reason) and checks them at import.
"""

from trust import HTTPS, _e

GAME = "PALWORLD"
#: Server variants; Palworld has none.
VARIANTS: frozenset[str] = frozenset()
PAL = frozenset({GAME})

#: Every entry is a name a world was observed using, and the minimum each
#: stage completed with behind a blocking guard (image v2.8.1, game v1.0.5,
#: 2026-11-01): none inferred from vendor documentation, and none from a
#: certificate probe.

#: The install container: SteamCMD downloading the dedicated server
#: (``SETUP_ONLY``). Steam is reachable from nowhere else.
INSTALL = (
    _e(PAL, "*.steamserver.net", "tls", HTTPS + ((27018, 27038),),
       "Steam connection managers SteamCMD logs in to. TLS websockets on 443 "
       "and 27018-27038, the ports Steam's own directory "
       "(ISteamDirectory/GetCMListForConnect) hands out; names rotate per region."),
    _e(PAL, "*.steamcontent.com", "tls", HTTPS,
       "SteamCMD downloads the Palworld server's content from Steam's CDN; "
       "names rotate per cache."),
    _e(PAL, "client-update.steamstatic.com", "tls", HTTPS,
       "SteamCMD updates itself before installing anything. Refused, SteamCMD "
       "aborts and the image falls back to its bundled DepotDownloader: that "
       "installed, but it is the image's recovery path, not its normal one."),
)

#: The running server: player authentication and crossplay. It makes no Steam
#: traffic (the world is unlisted), so a mod cannot reach Steam either.
PLAY = (
    _e(PAL, "auth.game.pocketpair.jp", "tls", HTTPS,
       "Pocketpair's player authentication; joins fail without it."),
    _e(PAL, "api.epicgames.dev", "tls", HTTPS,
       "Epic Online Services, which Palworld uses for crossplay sessions. "
       "Looked up at boot and on join; retried ~48x/min when refused."),
)

#: Denied on purpose. None of these affect gameplay.
DENY = (
    _e(PAL, "o1291919.ingest.us.sentry.io", None, (),
       "Palworld ships crash reports to a third-party error-tracking SaaS. The "
       "digits are Pocketpair's Sentry org id and change if they move project."),
    _e(PAL, "telemetry.palworldgame.com", None, (),
       "Pocketpair's gameplay telemetry."),
    _e(PAL, "b.palworldgame.com", None, (),
       "More Pocketpair telemetry, and the stock ban list's host. A player "
       "joined and chatted with it refused."),
    _e(PAL, "api.github.com", None, (),
       "The Palworld image's container-update check (every boot and install, "
       "no opt-out). Worlds run with it refused."),
    # --- Refused only while installing: SteamCMD asks, and installs anyway.
    _e(PAL, "api.steampowered.com", None, (),
       "SteamCMD's connection-manager directory. Refused, it falls back to the "
       "list it ships with (installed and booted without it).", stages={"install"}),
    _e(PAL, "clientconfig.akamai.steamstatic.com", None, (),
       "SteamCMD's client config, plain HTTP. Installed and booted without it.",
       stages={"install"}),
    _e(PAL, "steamconnecttest.com", None, (),
       "Steam's captive-portal probe, plain HTTP, made by the running server "
       "at boot. The server runs without it; installs never needed it."),
)
