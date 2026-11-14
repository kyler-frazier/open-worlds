# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## What this is

A self-hosted platform for running game servers (Minecraft and Palworld today)
on one machine: a FastAPI control plane, a worker, a React SPA, and a Docker
Compose stack that runs it all, plus one container per world.

## Layout

| Path | Purpose |
| --- | --- |
| `api/` | Control plane: API, worker and world manager. One Python package (`open_worlds`), one image. |
| `api/src/open_worlds/common/games/<game>/` | Everything one game needs. Nothing outside names a game. |
| `api/migrations/` | Alembic migrations. Production runs exactly these. |
| `web/` | The SPA (React, Vite). Per-game presentation in `web/src/games/`. |
| `deploy/` | Compose stack, edge nginx, egress guard, DNS observer, monitoring, host config. Per-game routers in `deploy/games/<game>/`. |
| `docs/` | Guides that span directories, such as `adding-a-game.md`. |

Each directory has its own README. `deploy/RUNBOOK.md` is the operations manual.

## Commands

```sh
# api/
uv sync --dev
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest            # route tests start a throwaway Postgres in Docker

# web/
npm ci
npm run lint && npm run format:check && npx tsc --noEmit && npx vite build

# deploy/
docker compose --env-file .env.example --profile '*' config >/dev/null
```

`CONTRIBUTING.md` has the full list CI runs, and the local dev setup (a dev-only
sign-in that needs no Google project).

## Invariants (each one fixes a real problem)

- **The database is the source of truth.** Containers are disposable and rebuilt
  from the world row. Never store state only in a container.
- **Fail closed.** A setting that would be unsafe when missing has no default,
  and the process refuses to start without it. Never add a fallback that turns a
  missing secret or a typo into an open door.
- **Games stay behind their adapter.** Anything that differs between games is a
  `GameAdapter` surface or hook, in the game's own package. An
  `if game == ...` anywhere else is a bug in the seam; `docs/adding-a-game.md`.
- **World containers are untrusted.** They run other people's mods: no host
  ports, no Docker socket, user-namespace remapped, egress through a guard.
  Uploads never carry executable code.
- **Only the world manager holds a Docker credential**, and only through the
  socket proxy. The api and worker ask it.
- **One stack per Docker host.** Two copies delete each other's worlds as
  orphans. Never start a second stack on a machine that runs one.
- **No API schema in any environment.** OpenAPI and docs stay off.
- **Closed garden.** An unapproved sign-in creates no database rows.

## Conventions

- Comments explain why, not what. Keep them as short as the reason allows.
- A fix comes with the test that would have caught it. Queries are tested
  against real Postgres, not a fake session.
- Container environments must not change by accident: a changed template
  rebuilds every world's container on its next start.
- [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`,
  `docs:`, ...), one logical change each, `Fixes #N` to close an issue.
- Update `CHANGELOG.md` under _Unreleased_ for user-visible changes.
- No new dependency without a reason that is written down.
