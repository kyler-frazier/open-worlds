# open-worlds: web

Single-page app (Vite + React + TypeScript) for the multi-game server management
platform. Talks to the api ([`api/`](../api/)) over same-origin `/api` using cookie-based
sessions.

One login, one dashboard, both games. A world's **game** decides which controls
the UI offers, and the SPA learns that from the API rather than hard-coding it:
`GET /worlds/limits` returns a per-game capability list, and the create and edit
forms are built from it. Adding a game to the backend surfaces here with no
frontend change beyond a display label.

## Screens

- **Login** — "Sign in with Google" (redirects to the API's OAuth flow).
- **Pending** — shown to unapproved users; displays the Google `sub` to hand to
  an admin (copy button). No account state is created until approval.
- **Dashboard** — the user's worlds of every game with live status, player
  counts, start/off/delete/backup controls, the connect address, and a
  Drive-link prompt. Each card is badged with its game.
- **Admin** — allow-list management (approve by pasting a `sub`, revoke) and a
  global view of all worlds filterable by owner, with the same controls.

## Development

```bash
npm install
npm run dev      # http://localhost:5173, proxies /api -> http://localhost:8000
npm run build    # type-check + production build into dist/
npm test         # unit and component tests (Vitest)
```

## Tests

Vitest with Testing Library, in jsdom. Tests sit beside the file they cover
(`worldList.test.ts`). Pure logic is tested directly; components are rendered
and driven the way a user would, with the `api` methods they call spied on
(`vi.spyOn(api, "startWorld")`) rather than a fake server. `src/test/` has
fixtures for a complete `World`, `Me`, `GameInfo` and `Limits`, and
`renderSignedIn` for components that need the auth and games providers.

No snapshot tests: they fail on every markup change and get updated without
being read.

## Auth model

The SPA never handles tokens. Login and Drive linking are full-page redirects to
the API, which sets a signed, `HttpOnly` session cookie. All API calls use
`credentials: "include"`. On a `401` the app shows the login screen.

## Production

The container builds the bundle and serves it with nginx (SPA history fallback).
TLS and the `/api` proxy are handled by the edge reverse proxy in
[`deploy/`](../deploy/).

## What the game decides

|                        | Minecraft                                              | Palworld                                        |
| ---------------------- | ------------------------------------------------------ | ----------------------------------------------- |
| Connect address        | `world.servers.example.com`                            | `world.servers.example.com:8213`                |
| Version + type pickers | shown                                                  | hidden (no version manifest, no loaders)        |
| Content add-ons tab    | shown                                                  | hidden (no trusted registry to curate from)     |
| Join password field    | hidden (access control is a username whitelist)        | shown, optional                                 |
| Sleep/wake copy        | "the first join takes a few extra seconds"             | "that first attempt will fail — try again once" |
| Memory pre-fill        | 4096 MB (80% reserved as JVM heap the moment it boots) | 8192 MB (only a ceiling it grows into)          |

Every row comes from the `games[]` capability list in `GET /worlds/limits`, not
from hard-coded knowledge here, so a third game surfaces with no change beyond a
display label.

That last row matters: Palworld's UDP protocol gives no channel to say "waking,
hold on", so the first connection to a sleeping world genuinely fails. Saying so
plainly is better than letting a player conclude the server is broken — and the
card offers **Wake now** on a sleeping world so an owner can start it a minute
before their friends join and sidestep the failed attempt entirely.

Join passwords are shown masked with a reveal toggle, in the create and edit
forms and on the card. Masked because a dashboard is often on a shared screen;
revealable because — unlike the platform-generated admin password, which nobody
ever sees — a join password is only useful once the owner passes it on.
