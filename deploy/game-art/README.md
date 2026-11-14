# Game art

Optional artwork for the world cards, one image per game. None is shipped: the
games' art belongs to their publishers, so each operator supplies their own.

Put a file here named for the game's id in lower case, with one of these
extensions: `.webp`, `.png`, `.jpg`, `.jpeg`, `.avif`.

```
deploy/game-art/minecraft.webp
deploy/game-art/palworld.png
```

The api serves it to signed-in users and the card shows it as a faded
watermark. A game with no file here simply has no art. Everything in this
directory except this README is git-ignored.

- **Another directory:** set `GAME_ART_DIR` in `.env` to its path on the host.
  It is mounted read-only into the api.
- **Running the api directly** (not under compose): `PLATFORM_GAME_ART_DIR`
  names the directory, and `PLATFORM_GAME_ART_<GAME>` (for example
  `PLATFORM_GAME_ART_MINECRAFT=box-art.png`) points one game at a different
  file, absolute or relative to the directory. Under compose, rename the file
  in this directory instead: the container can only see what is mounted.

Changes show up on the next page load; no restart needed.
