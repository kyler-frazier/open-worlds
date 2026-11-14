# open-worlds: api

Control-plane **API** and background **worker** for the multi-game server
management platform. One Python package, two run-modes, one Docker image.

- **API** (`platform-api`): FastAPI app that authenticates users (Google OIDC),
  enforces the closed-garden allow-list, and performs world CRUD + lifecycle
  actions by driving the Docker Engine API.
- **Worker** (`platform-worker`): APScheduler + a serial job consumer that runs
  backups (to each user's Google Drive), world imports, curated-content installs,
  and live player-count polling.

The **database is the source of truth**. Containers are ephemeral and fully
described by their world record, so any world container can be recreated from
Postgres. Managed containers are identified by Docker **labels**, not by name.

## Game type is first-class

A world's `game` is a column, not a flag checked in a dozen places. Everything
that differs between games is declared on a `GameAdapter`, one package per game
under `common/games/<game>/`, registered through the `open_worlds.games` entry
point. The rest of the platform is written once against that interface. If a
change needs an `if game is ...` outside that package, the seam is in the wrong
place. [docs/adding-a-game.md](../docs/adding-a-game.md) is the checklist.

These differ, and each is a surface on the adapter:

| Surface | Minecraft | Palworld |
|---|---|---|
| Container spec | `itzg/minecraft-server`, mounts `/data` | `palworld-server-docker`, mounts `/palworld` |
| Addressing | one shared TCP port; mc-router picks a backend from the hostname in the handshake | one UDP port per world; the protocol has no hostname, so the port *is* the routing key |
| Admin protocol | Source RCON | REST API (RCON is deprecated upstream) |
| Import policy | keep the world, strip jars | keep `Pal/Saved/`, strip the game install and every `.pak`/`.dll`/script |
| Capabilities | pins versions, has a curated catalog | neither: no version manifest, no trusted registry |
| Default memory | 4 GiB, *reserved* — 80% becomes the JVM's `-Xms`/`-Xmx`, the rest is off-heap headroom under the container limit | 8 GiB, only a cgroup ceiling the server grows into |
| World settings | `server_type`, `version` | `server_description` |
| Brings along | mc-router route publishing and idle sleep (worker jobs), the curated content catalog (routes, jobs, restore hooks) | nothing |

A world's settings are a JSON column (`worlds.game_settings`) validated by the
adapter's pydantic `settings_model`, so no game adds a column. `GET /api/games`
publishes each enabled game's capabilities and the JSON schema of its settings,
which is what the web app builds its forms from.

Adding a game means writing one adapter and registering it. `tests/test_games.py`
holds contract tests every adapter must satisfy, and `tests/test_fake_game.py`
runs a game defined only in the test through a world's whole life.

### What that buys the rest of the code

`world_status.py`, `maintenance.py`, `retention.py`, `authz.py`, `crypto.py`,
`drive.py`, the worlds API and the job queue are game-agnostic. The orchestrator builds containers without knowing what
runs in them; `upload_inspect.py` holds the prune *mechanism* while each adapter
holds the *policy*.

### World state model
| DB state   | Container         | Meaning                                             |
|------------|-------------------|-----------------------------------------------------|
| `RUNNING`  | running           | Up and serving players.                             |
| `SLEEPING` | exited, present   | Idle; the router auto-stopped it and will wake it.  |
| `STOPPING` | running           | Shutting down gracefully; becomes `OFF` or `SLEEPING`. |
| `DELETING` | being removed     | Transient; the sweep finishes a wedged delete.       |
| `OFF`      | absent            | Explicitly stopped; container removed, won't wake.  |
| `ERROR`    | —                 | Last lifecycle action failed.                       |

Turning a world **off** removes its container so neither router will wake it;
data lives on the host bind mount (`worlds/<subdomain>/data`) and is untouched.
Both off and sleep are **two-phase**: the request records `STOPPING` and the
target, and the graceful shutdown (minutes, for Palworld) runs as a background
task, or in the worker's sweep if the API process died first. `STOPPING` is
persisted rather than derived because during the window the container is still
running, and a reconcile that only read the container would call it `RUNNING`
and fight the stop. Readiness (`STARTING`) is the opposite case, an observation
layered on a true `RUNNING`, which is why it stays derived. Deletes skip the
graceful window: the data is about to be removed, so there is nothing to save.
The mapping is identical for both games — a graceful `docker stop` exits 143
whoever issues it — which is why `world_status.py` needed no changes.

## Layout

```
src/open_worlds/
  common/
    games/      the game seam: base (adapter, import policy, settings) and
                the registry
      minecraft/  adapter, settings, config, rcon, versions, routing
                  (mc-router), content catalog (content, content_api,
                  content_jobs)
      palworld/   adapter, settings, config, restapi, settings_file
    config, runtime_config, logging, db, models, crypto, orchestrator,
    ports (per-world port allocation), drive, subdomain, retention, authz,
    upload_inspect, metrics
  api/          FastAPI app, routers (auth, worlds, games, admin,
                collaborators, internal), deps, oauth, schemas
  worker/       scheduler + job consumer, backup/import/content execution,
                player-count polling, maintenance sweep
migrations/     Alembic migrations
tests/          pytest suite (no live DB/Docker required)
```

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --dev          # create .venv from the committed lockfile
uv run pytest          # run the unit tests
uv run ruff check .    # lint
```

### Database migrations

```bash
uv run alembic upgrade head
```

### Clicking through locally (dev login)

`PLATFORM_DEV_LOGIN=true` adds `/api/auth/dev/login?role=admin|user`, which
signs you in as a fake `dev:admin` / `dev:user` without Google. The login page
shows matching buttons under `npm run dev`. The api **refuses to start** with it
on unless it is outside a container, the web origin, OAuth base and database
are all loopback, and secure cookies are off; it then listens on 127.0.0.1
only. Deploy preflight rejects a `.env` that sets it. See `dev_login.py`.

```bash
docker run -d --name open-worlds-dev-db -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=dev -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=dev postgres:16-alpine
```

`api/.env` (gitignored), on top of the secrets in `.env.example`. The Google
client id/secret can be any placeholder:

```bash
PLATFORM_DEV_LOGIN=true
PLATFORM_DATABASE_URL=postgresql+psycopg://dev:dev@127.0.0.1:5432/dev
PLATFORM_WEB_ORIGIN=http://localhost:5173
PLATFORM_OAUTH_REDIRECT_BASE=http://localhost:5173
PLATFORM_COOKIE_SECURE=false
PLATFORM_LOG_JSON=false
```

Then `uv run alembic upgrade head`, `uv run platform-api`, and `npm run dev` in
`web/`. Starting, stopping and creating worlds still needs the world manager and
Docker, so this covers the dashboard, auth and admin screens, not world lifecycle.

## Configuration

All settings are environment variables prefixed `PLATFORM_` (see `.env.example`).
Secrets (`PLATFORM_SESSION_SECRET`, `PLATFORM_FERNET_KEY`, Google OAuth
credentials) have **no defaults** — the process refuses to start without them.

Which games are offered is an admin setting (Admin > Settings > Games), not an
environment variable; `common/game_availability.py` holds the rules. A game
with worlds stays offered, at least one always is, and a game can only be
turned on while its router runs (`PLATFORM_COMPOSE_PROFILES`).

`PLATFORM_MIN_MEMORY_MB` and `PLATFORM_MAX_MEMORY_MB` are the host's envelope and
apply to every game. A world's figure is always its container's memory limit,
with swap disabled; what the game does inside that limit is the adapter's
business (Minecraft hands 80% to the JVM heap and keeps the rest for off-heap).
`PLATFORM_CONTAINER_CPUS` optionally caps each world's CPU cores; it is unset by
default because the right figure depends on the host. `PLATFORM_HOST_MEMORY_BUDGET_MB`
caps the *sum* of running and sleeping worlds' limits (a sleeping world wakes
whenever a player connects, so its memory is already promised): a start or wake
that would exceed it is refused with a clear message. Set it -- without it, anyone who can wake a
world can oversubscribe the host. `PLATFORM_DEFAULT_MEMORY_MB` is deliberately **unset** by
default: what a *new* world starts at is a per-game property (the sixth adapter
surface above), because the figure is reserved heap for one game and a mere
ceiling for another. Set it only to force a single figure on every game; a
per-game default outside the envelope is clamped into it rather than refused,
since nobody chose that number.

## Security notes

- **Sessions**: signed cookies (`itsdangerous`), `HttpOnly`, `Secure`,
  `SameSite=Lax`; the cookie holds only the user id and a **session version**.
  The version is compared against the user row on every request, so bumping
  it ends every session the user holds: "Sign out everywhere" for the user,
  and an admin's force-logout that keeps the account intact.
- **Drive tokens**: refresh tokens are encrypted at rest with Fernet
  (`PLATFORM_FERNET_KEY`); a database leak alone cannot touch a user's Drive.
- **Drive scope**: `drive.file` only — the app can see only files it created.
- **Docker access**: the API talks to a **docker-socket-proxy**, never the raw
  socket. Container specs are built from a fixed, validated template; users never
  supply arbitrary Docker parameters.
- **No world container publishes a host port**, whichever game it runs. Both
  routers reach containers over the private network, so a compromised control
  plane cannot make Docker bind a port of its choosing.
- **Admin passwords** are generated per world and encrypted at rest. The admin
  interface (RCON or REST) is never published to the host; only the worker
  reaches it, over the private `games` network. Nobody, including the owner,
  ever sees this password.
- **Join passwords** are a different thing and are handled differently: they are
  the owner's to choose and to share, so they are encrypted at rest but decrypted
  back into the world response for the owner and admins. Only games whose server
  has the concept (`supports_server_password` — Palworld) accept one; Minecraft's
  access control is `whitelist.json`, not a shared password, and setting one there
  is rejected rather than ignored. An unset password means an open world.
- **Internal API auth**: `/api/internal/*` requires `PLATFORM_INTERNAL_SECRET`,
  which is a required setting with no default. `internal-api` has to sit on the
  same Docker network as untrusted world containers to be reachable by the two
  routers, so this check *is* the boundary — an empty value would be an open
  wake/sleep API, which is why the process refuses to start without it.
- **No API schema, ever**: Swagger UI and `/openapi.json` are disabled in every
  environment, not gated on an environment string that can be typo'd into
  disclosing the whole control-plane surface through the public edge.
- **Closed garden**: an unapproved sign-in creates **no** database rows.
- **Admins manage, owners fill.** An admin can start, stop, edit, back up and
  delete any world, but only the owner can import into one or restore a backup
  into it, and a restore may only draw on a backup of the caller's own worlds.
  Backups an admin triggers still go to the owner's Drive under the owner's
  token; nothing gives an admin the archive.
- **Import safety**: uploads are size-capped, extracted with path-traversal
  protection, and zip symlink members are rejected.
- **Uploads never carry executable code.** Each game's import policy strips its
  own loadable formats wherever they sit. Minecraft exempts jars the platform
  installed itself (a curated catalog vouches for them, by sha512); Palworld has
  no catalog and therefore no exemption at all.
- **Palworld worlds never auto-update** (`UPDATE_ON_BOOT=false`): with no version
  manifest, an in-place update is the one thing that can silently change a
  world's game version underneath its save.

See [`deploy/`](../deploy/) for the full exposure map, the two
routers, and the edge/TLS configuration.
