# Adding a game

A game is one package in the API, at most one module in the web app, and at
most one compose file. Nothing else in the platform names a game. If adding
one seems to need an `if game == ...` somewhere else, the seam is in the wrong
place: add a hook to `GameAdapter` instead.

[`api/tests/test_fake_game.py`](../api/tests/test_fake_game.py) is the worked
example. It defines a whole game in one test file, registers it, and runs a
world through create, start, backup and stop with the real routes and worker.
Start by copying it.

## 1. The adapter (`api/src/open_worlds/common/games/<game>/`)

Subclass `GameAdapter` (`common/games/base.py`, which documents every field).
The parts every game has to decide:

| Surface | What to set |
|---|---|
| Container | `image` (pinned by digest), `data_mount`, `container_caps`, `environment()` |
| Addressing | `allocates_port` (true when the protocol carries no hostname), `container_port`, `connect_address()` |
| Admin protocol | `admin_client()`: flush a save, list players, report a version |
| Lifecycle | `stop_timeout_seconds`, `readiness_timeout_seconds`, `stops_only_via_platform`, `fatal_log_patterns` |
| Uploads and backups | `import_policy`: what is world data, what is regenerable, what is executable and always stripped |
| Memory | `default_memory_mb`, and whether it is reserved or only a ceiling |
| World settings | `settings_model`: a pydantic model with `extra="forbid"`; `spec_fields` for those that need the world off |

Then the optional hooks, each with a default that does nothing:

- `new_settings`, `updated_settings`, `settings_risks`, `check_settings_change`,
  `settings_schema`: defaults the game decides at runtime, and what a change
  risks. Minecraft fills in the latest release here.
- `config()`: the game's own `PLATFORM_*` variables, as a `GameConfig`
  subclass (`common/config.py`). Remember to pass them through in its compose
  file, or preflight refuses them.
- `periodic_jobs()`: worker jobs on a timer. Minecraft publishes mc-router's
  routes and sleeps idle worlds here.
- `api_routers()`, `job_handlers()`, `spec_extras()`,
  `managed_content_hashes()`, `after_restore()`: a feature only this game has.
  Minecraft's curated content catalog is all of these.
- `egress_variant()`: which of the game's egress allow-list entries apply to a
  world (a Minecraft loader).
- `install_needed()`, `install_environment()`, `finish_install()`,
  `run_environment()`, `install_hidden_paths`: an install stage. A game whose
  image downloads its server at boot installs it in a container of its own,
  behind the game's `INSTALL` egress list, then runs it behind `PLAY`, which
  lists only what players need. `install_needed()` decides from the data dir
  (Palworld: the server's files; Minecraft: a marker `finish_install()`
  writes). Without them a game has no install stage and its whole boot runs
  behind `PLAY`.
- `settings_from_archive()`: settings an uploaded world carries, so an import
  can adopt them.

The id (`game = "MYGAME"`) is upper case and permanent: it is stored on every
world, it names the game in the admin's Games list, and it labels the game's
containers.

## 2. Register it

In `api/pyproject.toml`:

```toml
[project.entry-points."open_worlds.games"]
mygame = "open_worlds.common.games.mygame:MyGameAdapter"
```

then `uv sync`. Built-in games register the same way, so moving one into its
own distribution later is only packaging.

## 3. Test it

- The contract tests in `tests/test_games.py` run against every registered
  adapter automatically.
- Add the game to `test_fake_game.py`'s shape: create, start, back up, stop.
- The egress guard refuses a game it has no policy for, and
  `test_the_guard_policy_knows_every_game_and_server_type` fails until there is
  one: add `deploy/games/<game>/egress.py` (copy another game's) with the
  game's `GAME`, `VARIANTS`, `INSTALL`, `PLAY` and `DENY`, from observation
  (see the RUNBOOK's *Egress guard* section). Find the minimum per stage: run
  a fresh install and a boot behind a blocking guard, then remove entries one
  at a time until each stage fails.

## 4. The web app (optional)

With no web code, the game already works: `/api/games` publishes its
capabilities and the JSON schema of its settings, and the create and edit
forms are built from that.

`web/src/games/<game>/` adds presentation: where its card art is anchored, the
tags on a world's card, what waking a world feels like to a player, upload
help, and any extra tabs. Register the module in `web/src/games/index.ts`. `GameModule` in
`web/src/games/types.ts` documents each field.

Card artwork is never shipped: operators add `deploy/game-art/<game>.webp` (see
its README), and the API serves it.

## 5. Deploy (if the game needs its own router)

Put the service in `deploy/games/<game>/compose.yml` under a profile named for
the game, and include the file from `deploy/docker-compose.yml`. Hosts turn it
on with `COMPOSE_PROFILES` in `.env`; preflight refuses an enabled game whose
profile is missing. Note the ports it publishes in the file's header, and
forward them on the router.

A game whose worlds can be reached through an existing router needs none of
this.

## 6. Enable it

Add its profile to `COMPOSE_PROFILES` on the hosts that should offer it, then
turn it on under Admin > Settings > Games. A new game is off until then
(`enabled_by_default` is for the platform's original game only), and the panel
lists anything `enable_problems` says is missing.
