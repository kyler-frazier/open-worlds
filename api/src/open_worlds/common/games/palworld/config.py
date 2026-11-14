"""Settings only Palworld reads. Same ``PLATFORM_*`` names as ever."""

from __future__ import annotations

from ...config import GameConfig, game_config


class PalworldConfig(GameConfig):
    # The REST admin API, reachable only on the private Docker network.
    rest_api_port: int = 8212
    # Sets Pal.AllowNegativeDeltaTime=1, via the image's
    # PALWORLD_ALLOW_NEGATIVE_DELTA_TIME. On a host whose clock steps back, an
    # empty server treats a backwards frame as fatal and segfaults; this
    # substitutes ~10ms for that frame. Masks the symptom, and a game update
    # may drop the flag, so off unless a host needs it.
    palworld_allow_negative_delta_time: bool = False


def palworld_config() -> PalworldConfig:
    return game_config(PalworldConfig)
