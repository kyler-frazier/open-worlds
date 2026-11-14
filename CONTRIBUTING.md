# Contributing

Thanks for helping out! Bug reports, ideas and pull requests are all welcome.
Use the [issue forms](../../issues/new/choose); security problems go through
[SECURITY.md](SECURITY.md) instead. Everyone here follows the
[code of conduct](CODE_OF_CONDUCT.md).

## Using AI tools

AI tools are welcome here: this project itself is written with AI assistance.
What matters is that a person stands behind every contribution.

- **Disclose it.** If AI helped with a pull request, say which tool and roughly
  how much in the pull request description.
- **Understand it.** You should be able to explain every change you submit and
  how it fits the rest of the platform without the AI's help.
- **Verify it.** Run the checks below. For bug reports, reproduce the problem
  yourself.
- **Edit it.** AI-written issues and descriptions tend to be long and padded.
  Trim them to what a maintainer needs to know.

Contributions that don't meet this bar may be closed without detailed review.
[AGENTS.md](AGENTS.md) is the briefing for AI agents, and a useful map for
people too.

## Setup

- [uv](https://docs.astral.sh/uv/) for `api/` (it installs the Python in
  `api/.python-version`).
- Node 26 and npm for `web/`.
- Docker, for the route tests (they start a throwaway Postgres) and for
  anything under `deploy/`.

```bash
cd api && uv sync --dev
cd web && npm ci
```

## Running it locally

`api/README.md` ("Clicking through locally") starts the api against a local
Postgres with a dev-only sign-in that needs no Google project, and `npm run dev`
in `web/` serves the UI with hot reload. The dev sign-in refuses to start
anywhere that does not look like a workstation, and deploy preflight refuses a
`.env` that sets it.

World lifecycle (start, stop, create) needs the world manager and Docker. The
full stack runs from `deploy/` (see its README), but **only one copy of the stack
may run per Docker host**: two copies remove each other's world containers as
orphans. Don't run it on a machine that already hosts a deployment.

## Checks

CI runs exactly these, and a change should pass them before it is pushed:

```bash
# api/
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run vulture           # dead code
uv run pytest

# web/
npm ci
npm run lint
npm run format:check     # npm run format fixes it
npm run knip             # unused files, exports, dependencies
npx tsc --noEmit
npm test
npx vite build

# deploy/
docker compose --env-file .env.example --profile '*' config >/dev/null
for f in scripts/*.sh; do bash -n "$f"; done
docker run --rm -v "$PWD:/d" -w /d mvdan/shfmt:v3.14.1 -i 2 -ci -d scripts   # -w to fix
for d in games/palworld/pal-waker egress-guard egress-exporter; do
  (cd "$d" && uvx --python 3.15 --with pytest-asyncio==1.4.0 pytest@9.1.1 -q)
done

# repo root: workflows and Dockerfiles
docker run --rm -v "$PWD:/r" -w /r ghcr.io/zizmorcore/zizmor:1.31.1 --offline .github/workflows
docker run --rm -v "$PWD:/r" -w /r hadolint/hadolint:v2.15.1 hadolint $(git ls-files '*Dockerfile')
```

A secret scan (gitleaks) runs over the whole history on every push. A false
positive is allowed in place with a `gitleaks:allow` comment.

## Style

- **Fail closed.** A setting that would be unsafe when missing has no default,
  and a process refuses to start without it rather than guessing.
- **Comments explain why.** The code says what it does; a comment gives the
  reason or the constraint, in as few lines as it takes. The story of how it
  was found belongs in the commit message.
- **Formatters decide layout:** ruff (api), Prettier (web), shfmt (deploy
  scripts). CI checks all three.
- **One word per thing.** A *world* is what a user owns; its *server* is the
  game process inside it. A world is turned *on* or *off*, and *sleeps* and
  *wakes* by itself. *Add-ons* are curated mods and packs; a world's *content*
  is its save data.
- **Games stay behind their adapter.** Anything that differs between games
  belongs on the game's adapter in `api/src/open_worlds/common/games/`, not in
  an `if game == ...` elsewhere.
- **Tests for the bug, not the line.** A fix comes with the test that would
  have caught it. Queries are tested against a real database, not a fake
  session that ignores the predicate.

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `chore:`, with a scope when it
  helps (`fix(api): ...`). One logical change per commit.
- The body says what was wrong and why the change fixes it. `Fixes #N` closes
  the issue it resolves.
- Pull requests are squash-merged, so the PR title becomes the commit subject
  and follows the same format.
- Add a line to `CHANGELOG.md` under _Unreleased_ for user-visible changes.
