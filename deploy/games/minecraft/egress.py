"""Minecraft's egress policy: what its worlds may reach, and what they may not.

Loaded by egress-guard/trust.py, which documents the rules every entry
follows (protocols, ports, stages, a required reason) and checks them at import.
"""

from trust import HTTPS, _e

GAME = "MINECRAFT"
#: Server variants (the API's ServerType); an entry may apply to some only.
VARIANTS = frozenset({"VANILLA", "PAPER", "FABRIC", "FORGE"})
MC = frozenset({GAME})

#: Every entry is a name a world was observed using, and the minimum each
#: stage completed with behind a blocking guard (Minecraft 26.3, 2026-11-01):
#: none inferred from vendor documentation, and none from a certificate probe.

#: The install container: the image's setup, plus one launch that exits before
#: loading the world (Paper patches its jar, Fabric caches its libraries).
INSTALL = (
    # --- Mojang: the version manifest and the vanilla server every loader
    # builds on. Paper finds its jar without the manifest.
    _e(MC, "launchermeta.mojang.com", "tls", HTTPS,
       "Version manifest the image and loader installers read to find a version.",
       {"VANILLA", "FABRIC", "FORGE"}),
    _e(MC, "piston-meta.mojang.com", "tls", HTTPS,
       "Per-version metadata the manifest points to.", {"VANILLA", "FABRIC", "FORGE"}),
    _e(MC, "piston-data.mojang.com", "tls", HTTPS,
       "Download of the vanilla server jar, which every loader builds on."),
    # --- Per loader.
    _e(MC, "fill.papermc.io", "tls", HTTPS,
       "Paper's build lookup and download service.", {"PAPER"}),
    _e(MC, "fill-data.papermc.io", "tls", HTTPS,
       "Paper's download storage behind fill.papermc.io.", {"PAPER"}),
    _e(MC, "meta.fabricmc.net", "tls", HTTPS,
       "Fabric's version metadata, read to pick the loader.", {"FABRIC"}),
    _e(MC, "maven.fabricmc.net", "tls", HTTPS,
       "Fabric loader and its libraries.", {"FABRIC"}),
    _e(MC, "files.minecraftforge.net", "tls", HTTPS,
       "Forge's version index, read to pick the installer.", {"FORGE"}),
    _e(MC, "maven.minecraftforge.net", "tls", HTTPS,
       "Forge installer and libraries.", {"FORGE"}),
    _e(MC, "libraries.minecraft.net", "tls", HTTPS,
       "Java libraries the Forge installer downloads; the vanilla jar bundles "
       "its own, and Fabric's come from its maven.", {"FORGE"}),
)

#: The running server: player authentication, nothing else.
PLAY = (
    _e(MC, "api.minecraftservices.com", "tls", HTTPS,
       "Microsoft account services: the server's signing keys at boot, and "
       "player profiles and keys checked on join."),
    _e(MC, "sessionserver.mojang.com", "tls", HTTPS,
       "Online-mode join authentication; without it no player can join."),
)

#: Denied on purpose. None of these affect gameplay.
DENY = (
    _e(MC, "api.github.com", None, (),
       "A catalog Fabric mod's update check. Worlds run with it refused."),
    _e(MC, "bstats.org", None, (),
       "Bukkit/Paper plugin telemetry; TabTPS submits about three minutes in."),
    _e(MC, "raw.githubusercontent.com", None, (),
       "The Minecraft image seeds default Paper/Bukkit config from a third-party "
       "repo at install; refused, Paper writes its own defaults."),
    _e(MC, "*.github.com", None, (),
       "Same image-side fetches as raw.githubusercontent.com."),
    # --- Refused only while installing: looked up, never connected to.
    _e(MC, "maven.creeperhost.net", None, (),
       "CreeperHost's mirror, a fourth party the Forge installer tries first; "
       "refused, it falls back to Forge's own maven (26.3, 1.21.1 and 1.20.1 "
       "installed and booted).", {"FORGE"}, stages={"install"}),
    _e(MC, "authserver.mojang.com", None, (),
       "Forge's installer looks it up and never connects; installs complete "
       "with it refused.", {"FORGE"}, stages={"install"}),
    _e(MC, "sessionserver.mojang.com", None, (),
       "Forge's installer looks it up and never connects. Allowed while "
       "playing, for joins.", {"FORGE"}, stages={"install"}),
    # --- Refused only while playing: the install needs them.
    _e(MC, "discovery.minecraftservices.com", None, (),
       "Looks up where Mojang's services live, at every boot. The server falls "
       "back to the built-in addresses and boots without it.", stages={"play"}),
    _e(MC, "fill.papermc.io", None, (),
       "Paper's update check at boot. Updates come from a reinstall.",
       {"PAPER"}, stages={"play"}),
    _e(MC, "files.minecraftforge.net", None, (),
       "Forge's update check at boot. Updates come from a reinstall.",
       {"FORGE"}, stages={"play"}),
)
