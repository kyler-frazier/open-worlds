# open-worlds: deploy

Orchestration and infrastructure for the multi-game server management platform.
Minecraft and Palworld worlds run side by side on one host, under one control
plane, one database and one web UI. A world's **game** is a first-class property
that decides how it is built, addressed and edited.
Brings together three images on a single host with Docker Compose:

- **api** + **worker** — built from [`api/`](../api/)
- **web** — built from [`web/`](../web/)
- **edge** — the app's own nginx reverse proxy. It owns *application* routing
  (web vs `/api`, blocking internal-only paths) and is the single entry point to
  the stack. It listens on plain HTTP, published on `127.0.0.1:${EDGE_PORT}`
  (default 18080), so an outer/global proxy (the host nginx or a cloud LB) can
  terminate TLS and forward to it. `api` and `web` publish no host ports of their
  own — they're reached only through the edge on the Docker network.
- **db** (Postgres), **mc-router**, **docker-socket-proxy**, **offline-placeholder**
- **internal-api** — a minimal read-only service (same image as `api`) exposing
  only `/api/internal/internal-api`. It lives on the private `games` network
  so the offline-placeholder can resolve world existence, which lets the main
  `api` stay off that network entirely.
- **Monitoring** (always on, LAN-only): VictoriaMetrics (pull-scrapes each
  service's `/metrics`) → Grafana, plus cAdvisor. See [Monitoring](#monitoring-private).

## Architecture

```
                              Internet
                 ┌───────────────────┬────────────────────┐
      HTTPS 443  │                   │   TCP 25565 (Minecraft)
                 ▼                   ▼
         ┌───────────────┐    ┌───────────┐
         │  outer proxy  │    │ mc-router │  routes <sub>.servers.<domain>
         │ host nginx/LB │    │           │  by handshake; wakes sleeping worlds
         │ terminates TLS│    └─────┬─────┘
         └───────┬───────┘          │ (docker events + start/stop)
     HTTP 127.0.0.1:${EDGE_PORT}    │
                 ▼                  │
         ┌───────────────┐         │
         │  edge (nginx) │         │
         └──────┬────────┘         │
        / ──▶ web   /api ──▶ api   │
               │           │       │
               ▼           ▼       ▼
            ┌─────┐  ┌────────────────────┐   ┌───────────────────────┐
            │ web │  │ docker-socket-proxy │─▶│  Docker Engine         │
            └─────┘  └────────────────────┘   │  game-world-<sub> world  │
                          ▲                    │  containers (itzg)     │
             ┌────────────┴───────┐            └───────────────────────┘
             │ api / worker       │  RCON flush before backup
             ▼                    ▼
          ┌──────┐         ┌──────────────┐
          │  db  │         │ Google Drive │  (per-user, drive.file)
          └──────┘         └──────────────┘
```

## Two edges, one stack

Everything about hosting a world is shared. What differs is how players reach
one, and that difference is not a setting -- it is forced by the games'
protocols:

| | Minecraft | Palworld |
|---|---|---|
| Router | `mc-router` | `pal-waker` |
| Ports | one shared TCP 25565 | one UDP port per world (8211-8220) |
| Routes by | hostname in the handshake | the port itself |
| Discovers worlds from | container labels | the database, via `internal-api` |
| Docker grants | create/start/stop | **none** |
| World is off | placeholder answers with a MOTD | nothing answers at all |
| Waking | router holds the connection | first connect fails; reconnect once |

Both routers sit on the same `games` network and cannot pick up each other's
worlds: a Minecraft container carries no port label and a Palworld container
carries no `mc-router.host` label. Adding a game means adding an adapter in
`api/` and, if it needs one, an edge service here.

### mc-router has no Docker access

mc-router used Docker for three things. The platform does all three now, so it
holds no credential at all -- `IN_DOCKER: false`, no `DOCKER_HOST`, no
`AUTO_SCALE_*`, and no socket proxy of its own.

| What mc-router did | What does it now |
|---|---|
| discover worlds from container labels | the worker publishes routes to mc-router's REST API |
| start a world on connect | the offline-placeholder asks `internal-api` to wake it |
| stop an idle world | the worker, using `auto_stop_after_seconds` |

**A route exists exactly while a world is RUNNING.** Anything else -- asleep,
off, mid-boot -- has no route and falls through to mc-router's `DEFAULT`, which
is the offline-placeholder. That is already how OFF worlds behaved, and it is
what makes wake work: the placeholder is where a player lands.

Publishing is a diff against mc-router's current table rather than a reaction to
events, because mc-router holds routes in memory and starts empty after a
restart. A missed event would leave a world unreachable; a diff on a timer
repairs itself.

**The placeholder holds the connection.** A join to a sleeping world is not
refused: the placeholder asks the platform to wake it, waits for the world to
accept, then replays the handshake and login it had to read and relays the
socket. The player sees a slow join, which is what mc-router used to give them.

Only a *join* does this. A status ping still gets the MOTD, because the
multiplayer server list pings constantly and waking a world because someone
opened a menu is the abuse pal-waker's datagram heuristic exists to prevent.

mc-router's route API remains unauthenticated, so it is bound to a private
`routerapi` network that world containers are not on -- reachable only by the
worker that publishes routes and by VictoriaMetrics.

## Prerequisites

- A Linux host with systemd and **Docker Engine** with the Compose plugin
  ([install](https://docs.docker.com/engine/install/)). Not Docker Desktop.
- `git`, `make`, `python3`, and `envsubst` (Debian/Ubuntu: `gettext-base`).
- `sudo` once, for the host setup below.
- To sign in: a Google OAuth client ([Google OAuth setup](#google-oauth-setup)).
- For alerts: a Discord webhook URL. Preflight refuses to start without one,
  because alerts that go nowhere fail silently.

## Quick start

From a fresh clone:

```bash
cd deploy
cp .env.example .env && chmod 600 .env
```

Edit `.env`:

1. Replace every `CHANGE_ME`. Generation commands are in the comments above
   each one, and in [Secrets](#secrets-to-set-in-env) below.
2. Set the Google client id and secret, and `ALERT_DISCORD_WEBHOOK_URL`.
3. Set `PLATFORM_WEB_ORIGIN` and `PLATFORM_OAUTH_REDIRECT_BASE` to where the
   UI will be reached, and `PLATFORM_SERVERS_DOMAIN` to the wildcard domain
   worlds are addressed under. To try it on one machine before setting up
   DNS and TLS, use `http://localhost:18080` for both URLs, `localhost` for
   the servers domain, and `PLATFORM_COOKIE_SECURE=false`.
4. Size `PLATFORM_HOST_MEMORY_BUDGET_MB` and `PLATFORM_MAX_MEMORY_MB` to the
   machine (see the comments).

Then:

```bash
sudo scripts/host-config.sh --apply   # Docker userns-remap, and the data root
make build
make up                                # creates the games network, preflight, start
make verify                            # the running stack matches this checkout
```

`host-config.sh --apply` installs [`host/docker/daemon.json`](host/docker/daemon.json)
and restarts Docker. On a host that already runs containers, read the RUNBOOK's
*Container UID remapping* section first: remapping switches Docker to a new
storage root.

Open `PLATFORM_WEB_ORIGIN` and sign in with Google. The first sign-in shows your
Google `sub` on the pending screen; put it in `PLATFORM_ADMIN_GOOGLE_SUBS` and
`make up` again to become an admin ([Bootstrapping the first admin](#bootstrapping-the-first-admin)).

`make up` also starts the monitoring stack (Grafana, admin-only at
`/grafana/`; break-glass on `${LAN_BIND}:3000`).

Going public adds DNS (below), TLS ([TLS](#tls)), and port forwards for 25565/tcp
and the Palworld UDP range. For day-to-day operation (redeploys, logs, backups
and restore, migrating an existing world, the on-disk layout, troubleshooting)
see the [operations runbook](RUNBOOK.md).

## Secrets to set in `.env`

| Variable | How to generate |
|----------|-----------------|
| `POSTGRES_PASSWORD` | a strong random string (the superuser) |
| `POSTGRES_OWNER_PASSWORD` | a strong random string (schema owner, for migrations) |
| `POSTGRES_APP_PASSWORD` | a strong random string (what the app services connect as) |
| `PLATFORM_SESSION_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `PLATFORM_FERNET_KEY` | `python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
| `PLATFORM_INTERNAL_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` (shared secret for `internal-api`) |
| `PLATFORM_GOOGLE_CLIENT_ID` / `PLATFORM_GOOGLE_CLIENT_SECRET` | Google Cloud console OAuth client |
| `PLATFORM_ADMIN_GOOGLE_SUBS` | your Google `sub` (see below) to bootstrap admin |
| `PLATFORM_WORLD_MANAGER_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `GRAFANA_ADMIN_PASSWORD` | a strong random string |

## DNS

Two records point at this host's public IP:

| Record | Type | Purpose |
|--------|------|---------|
| `games.example.com` | A/AAAA | management web UI + API (HTTPS) |
| `*.servers.example.com` | A/AAAA | all worlds; routed by mc-router on TCP 25565 |

A **wildcard** `*.servers` record is required so every world subdomain resolves
without per-world DNS changes. mc-router selects the backend from the Minecraft
handshake — nginx is not involved in Minecraft traffic.

## Google OAuth setup

In the Google Cloud console OAuth client, add these **authorized redirect URIs**:

- `https://games.example.com/api/auth/google/callback` (login)
- `https://games.example.com/api/auth/drive/callback` (Drive linking)

Scopes used: `openid email profile` for login, and the narrow
`https://www.googleapis.com/auth/drive.file` for backups (added incrementally
only if a user opts into backups).

### Bootstrapping the first admin

`sub` is Google's stable per-user identifier. To find yours: sign in once — the
pending screen displays your `sub`. Put it in `PLATFORM_ADMIN_GOOGLE_SUBS` and
restart; on next login you're seeded as an admin and can approve others from the
Admin screen.

## TLS

TLS is terminated by an **outer proxy** that owns the certificate and forwards
to the stack's `edge`, which keeps listening on plain HTTP at
`127.0.0.1:${EDGE_PORT}` and does the app-level routing. Two ways to run it.

### The `tls` profile (Caddy, in the stack)

```bash
docker compose --profile tls up -d
```

Caddy is here rather than nginx+certbot because the certificate lifecycle is
the whole job: it obtains and renews via ACME by itself, with no cron, no
renewal hook, and no state to back up beyond one volume. Config is
[tls/Caddyfile](tls/Caddyfile).

It is **off by default**, because it cannot work until DNS points at this host
and 80/443 reach it — until then Caddy fails ACME on every start and
crash-loops. Enabling it does not move the existing entrance: the edge stays on
`127.0.0.1:${EDGE_PORT}` either way.

**Before enabling, all of this has to be true.** The first two are Caddy's; the
rest are the app's, and skipping them gives you a working padlock and a broken
login, because the API builds its OAuth redirect and its CORS/CSRF origin from
configuration rather than from the request.

1. An A/AAAA record for `PUBLIC_DOMAIN` pointing at this host.
2. Ports 80 and 443 reaching it. 80 is not optional — ACME's HTTP-01 challenge
   uses it.
3. `.env`:

   ```bash
   PUBLIC_DOMAIN=games.example.com
   ACME_EMAIL=you@example.com
   PLATFORM_OAUTH_REDIRECT_BASE=https://games.example.com
   PLATFORM_WEB_ORIGIN=https://games.example.com
   PLATFORM_COOKIE_SECURE=true
   ```

4. The same `https://games.example.com/api/auth/google/callback` added as an
   authorised redirect URI in the Google Cloud console.

`make preflight` refuses to start the profile unless `PUBLIC_DOMAIN` and
`ACME_EMAIL` are set, so a missing variable fails immediately rather than
looking like a networking problem.

While DNS is still settling, point Caddy at Let's Encrypt staging — production
allows only 5 failures per account per hostname per hour, and a mispointed
record burns through them quickly:

```bash
ACME_CA_DIRECTIVE="acme_ca https://acme-staging-v02.api.letsencrypt.org/directory"
```

Staging certificates are untrusted by browsers; that is the point. Clear the
variable and restart to get a real one.

HSTS is set here, on the proxy that owns TLS. Every other browser-hardening
header (CSP, nosniff, frame denial, referrer and permissions policies) is set
by the edge so it travels with the stack.

### A host nginx instead

If you would rather terminate on the host — a shared box, or an existing nginx
already serving other sites — leave the profile off and point it at the edge:

```bash
certbot certonly --nginx -d games.example.com
```

```nginx
server {
    listen 443 ssl;
    server_name games.example.com;
    # ssl_certificate / ssl_certificate_key from certbot ...

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    client_max_body_size 2g;          # allow world uploads

    location / {
        proxy_pass http://127.0.0.1:18080;   # ${EDGE_PORT}
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;             # a Palworld stop can take 300s
        proxy_request_buffering off;         # stream large uploads
    }
}
```

The `.env` and Google-console steps above apply either way.

### Game traffic is not affected

Minecraft (TCP 25565) and Palworld (UDP 8211-8220) are not HTTP and do not pass
through TLS. `PLATFORM_SERVERS_DOMAIN` keeps pointing at this host directly.

## Exposure map

| Port / surface | Bound to | Notes |
|----------------|----------|-------|
| 443 / 80 (outer proxy) | public | Host nginx / cloud LB terminates TLS and forwards to the edge |
| edge (nginx) | `127.0.0.1:${EDGE_PORT}` | App routing; default 18080. The outer proxy's only upstream |
| api / web | none | No published ports; reached only via the edge over the `edge` network. `api` is **not** on the `games` network |
| internal-api | none | internal `backend` + `socketproxy` + `games`; serves only `/api/internal/*`, requires the `PLATFORM_INTERNAL_SECRET` header |
| 25565 (mc-router) | public | Minecraft; routes by hostname only |
| mc-router API/metrics 26660 | none | bound to its static `socketproxy` address only; unauthenticated, so kept off `games` |
| Postgres | none | internal `backend` network; no published port |
| docker-socket-proxy | none | internal `socketproxy` network only |
| api `/metrics`, worker `:8001` | none | internal; blocked at the edge (`/metrics` → 404) |
| Grafana 3000 | `${LAN_BIND}` | LAN/VPN-only; default `127.0.0.1`. Never bind to a public interface |
| VictoriaMetrics / cAdvisor | none | internal `monitoring` network; VM scrapes targets over `backend`/`socketproxy` |

## Monitoring (private)

The metrics stack (VictoriaMetrics + cAdvisor + Grafana) starts with the rest
of the platform, so a reboot cannot leave alerting off.

**Pull model:** VictoriaMetrics scrapes each service's `/metrics` directly
(api, worker, world-manager, internal-api, mc-router, cAdvisor and the egress
exporter) over the private Docker networks; nothing pushes. The scrape
config lives in
[`monitoring/victoriametrics/scrape.yml`](monitoring/victoriametrics/scrape.yml).
Grafana auto-provisions the VictoriaMetrics datasource and the dashboards in
[`monitoring/grafana/dashboards/`](monitoring/grafana/dashboards/). Alerts are
listed in the [RUNBOOK](RUNBOOK.md#alerting).

### Accessing Grafana

Platform admins open **Admin > Metrics** (or `/grafana/` directly). The edge
asks the api about every Grafana request and lets only a current admin session
through, naming the user in a header Grafana trusts from the edge's address
alone (auth proxy). Admins are Grafana **Viewers** with `viewers_can_edit`:
dashboards and Explore, panels editable in place, nothing saved. An admin
"yes" is cached for 1 second per session cookie (`proxy_cache_valid` in
`edge/nginx.conf`), which is how long revoking admin or signing out
everywhere takes to bite.

Break-glass, for when the api is down: Grafana is also bound to `${LAN_BIND}`
(default `127.0.0.1`, **never** a public interface). Tunnel over SSH, open
<http://localhost:3000/grafana/> and log in with `admin` /
`GRAFANA_ADMIN_PASSWORD`:

```bash
ssh -L 3000:127.0.0.1:3000 <host>
```

Grafana joins a small non-internal `grafana-egress` network only so Docker can
publish its loopback port (Docker won't publish ports for a container on
internal networks alone); its analytics/update phone-home is disabled so it
makes no outbound calls.

## Security posture

- **The api cannot reach Docker at all.** Anything able to create a container
  can ask for one with `/` bind-mounted and be root on the host, and a path
  filter cannot prevent it -- the socket proxy never reads a request body, so
  "may create containers" and "may take over the machine" are one grant. The
  api is the worst place for it: it faces the internet, runs the OAuth exchange
  and extracts uploaded archives.

  So the credential lives in **`world-manager`**, which exposes six operations
  (start, stop, sleep, delete, inspect, list), each taking a subdomain or a
  world spec. A compromised api can ask for a well-formed world; there is no
  parameter with which to ask for a host mount. api, worker and internal-api are
  not on the `socketproxy` network and cannot resolve the proxy's name.

  `world-manager` is as privileged as the api used to be, so a flaw *there* is
  still a host compromise -- what shrank is the surface, from an internet-facing
  application to six typed endpoints on a private network behind a shared
  secret.

- **The socket proxy is still least-privilege** (containers + events + post;
  exec/volumes/secrets/swarm denied; socket mounted read-only), and additionally
  **denies any mutation that does not name a `game-world-*` container**.
  mc-router has its own proxy, cut down to start/stop only, since its auto-scale
  addresses containers by id. See [socketproxy/README.md](socketproxy/README.md).
- **Fixed container template.** World containers are built from a validated
  spec (image, memory limit, labels, bind mount, RCON). Users never pass
  arbitrary Docker parameters, so they cannot request privileged mounts,
  host networking, or capability grants.
- **Postgres** is authenticated and never published to the host/internet.
- **Sessions**: signed, `HttpOnly`, `Secure`, `SameSite=Lax` cookies holding
  only the user id.
- **Drive tokens** are Fernet-encrypted at rest (`PLATFORM_FERNET_KEY`); scope is
  limited to `drive.file`.
- **Online mode** (`ONLINE_MODE=TRUE`) is enforced on every world.
- **Closed garden**: unapproved sign-ins create no state.
- **Import uploads** are size-capped and extracted with path-traversal guards,
  and zip **symlink members are rejected**; imports also run a free-disk guard.
- **Uploads are pruned to a bare-essential world.** Only Tier 1 (world data +
  server state/config) and Tier 2 (modpack `config/`/`defaultconfigs/`, plus any
  admin-allowed names) survive. Regenerable caches/logs, executable script dirs
  (`kubejs/`, `scripts/`), and **all `*.jar` files** (loose or nested in
  `mods/`/`plugins/`) are stripped unless the jar is platform-managed. The user
  is shown exactly what will be removed and must confirm the prune, as today.
- **Monitoring is LAN-only** — never exposed to the public internet.
- **`.env` is owner-only** (`600`); `make` runs a preflight that refuses to start
  a stack whose secrets file is world/group-readable.
- **Each container gets only the secrets it uses.** App services no longer take
  `env_file: .env`, which handed every container every line of it (the Postgres
  and Grafana passwords included), visible via `docker inspect`. Non-secret
  config is passed through an explicit `x-platform-config` block; the api gets
  the session secret, Fernet key and OAuth client, the worker the Fernet key and
  OAuth client, internal-api the internal secret and Fernet key. Each process
  refuses to start without its own set, and `make preflight` refuses a
  `PLATFORM_*` line in `.env` that compose would not pass through -- then checks
  the other direction too, loading each service's config in its own container so
  a secret handed to the wrong service fails the deploy rather than the boot.
- **API docs are disabled in production** (`docs_url=None` when
  `PLATFORM_ENVIRONMENT=production`) so the schema is not served at `/api/docs`.
- **Container images are pinned by digest** (`itzg/minecraft-server`,
  `itzg/mc-router`) for reproducible, supply-chain-safe deploys.
- **The host has a memory budget.** `PLATFORM_HOST_MEMORY_BUDGET_MB` caps the
  sum of running worlds' memory limits; a start or wake that would exceed it is
  refused with a plain message rather than letting the kernel pick a victim.
- **A lone UDP packet cannot start a Palworld server.** pal-waker wakes a
  sleeping world only for a source that sends several datagrams within a few
  seconds (a client retrying its handshake), never a single scan probe, and a
  port will not wake its world twice within a cooldown. See the RUNBOOK's
  pal-waker section for the knobs.
- **mc-router's admin API is not on the `games` network.** It has no
  authentication and can add or delete routes, which from a world container
  would mean hijacking another world's hostname. It is bound to mc-router's
  static address on the private `socketproxy` network, where only
  VictoriaMetrics reads it.
- **The control-plane `api` is off the `games` network.** World containers
  (which may run untrusted mod jars) cannot reach the full API or `/metrics`
  directly; only the minimal `internal-api` service is on that network, and it
  requires a shared secret (`PLATFORM_INTERNAL_SECRET`).
- **World containers are hardened**: `no-new-privileges`, all Linux capabilities
  dropped except the minimal set the itzg entrypoint needs, and a `pids_limit`
  fork-bomb guard. Per-world data directories are `0700` so plaintext RCON
  sidecar files are not readable by other local accounts.
- **User-namespace remapping (`userns-remap`)** shifts every container UID by the
  `dockremap` base, so a process running as our `PUID:PGID` inside a container is
  an unprivileged high UID on the host. A world container that escaped could not
  act as a real host user. It is a daemon setting, kept in
  [`host/docker/daemon.json`](host/docker/daemon.json); preflight refuses to start
  without it and `make verify` checks it. `docker-socket-proxy` and `cadvisor` opt
  out (`userns_mode: host`) because they need the host's socket or `/sys`; the untrusted world containers stay
  remapped. See the RUNBOOK's *Container UID remapping* section.
- **World data lives under `PLATFORM_DATA_ROOT`** (compose default `/srv/games`),
  owned by the remapped range rather than by any login account.
- **Browser hardening headers at the edge.** The edge nginx sets a strict
  Content-Security-Policy on HTML (same-origin scripts, no frames either way),
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, a referrer policy
  and a permissions policy on every response. HSTS is the outer TLS proxy's to
  set (see the TLS section).
- **CSRF defense in depth.** Every mutating (`POST`/`PUT`/`PATCH`/`DELETE`)
  route requires a custom `X-Requested-With` header. Combined with
  origin-locked CORS and `SameSite=Lax` cookies, a cross-site request cannot
  forge a state change (custom headers force a CORS preflight, which the
  origin-locked policy rejects). The SPA sends this header on every request.
- **Uvicorn trusts forwarded headers only from the edge.**
  `PLATFORM_FORWARDED_ALLOW_IPS` is pinned to the edge proxy's static IP, so a peer
  that reaches uvicorn directly on the internal networks cannot spoof
  `X-Forwarded-For` to appear to come from a trusted client.
- **Dependency + image scanning.** `make scan` runs Trivy (image OS + language
  packages, HIGH/CRITICAL fixable) and `pip-audit` (API Python deps), and
  [Dependabot](../.github/dependabot.yml) opens weekly update
  PRs for pip/npm/Docker. Application dependencies are audited on demand;
  base-image OS-package findings are cleared by rebuilding on a refreshed base
  (`make build`) rather than by code changes. See the RUNBOOK for the workflow.

### Residual risks (accepted for a single-host personal deployment)

- **A compromise of the api process is host root.** The socket-proxy grants
  `CONTAINERS` + `POST`, and filters by URL path only -- never by request body.
  A raw `POST /containers/create` may therefore specify any `HostConfig`,
  including host bind mounts, `Privileged`, and `UsernsMode: host`, which also
  bypasses the userns remapping above. `EXEC: 0` and `VOLUMES: 0` do not
  constrain this: exec is unnecessary when `Cmd` is set at create time, and bind
  mounts are not volumes. This is inherent to a Docker control plane and is
  accepted here; the app's own code paths only ever build the fixed container
  template.
- All worlds and the control plane share one host; there is no multi-tenant
  isolation beyond containers and memory limits.
- Backups depend on each user's Google Drive quota and availability.

## World versions

Worlds are **pinned to a specific Minecraft version** — a release (e.g. `1.21.4`)
or a specific snapshot / pre-release (e.g. `24w14a`, `1.20.2-pre1`). Only the
**rolling** `LATEST`/`SNAPSHOT` aliases are rejected at create and edit time, so a
world can never silently auto-update into a game version that breaks its data or
mods (a pinned snapshot is fixed and won't drift). The create form pre-fills the
**latest stable release** as the default (resolved from Mojang's version manifest
— the same version itzg's `VERSION=LATEST` would install — cached, with a
configured fallback for manifest downtime); users can keep it, pin an older
release, or type a specific snapshot. Curated **mods** are only offered for
concrete releases (they resolve deterministically); datapacks and resource packs
are still available on snapshot worlds.

## Curated content catalog

Instead of letting users upload arbitrary mod jars, the platform installs
**admin-curated** content from a trusted source. There are three separately
curated kinds:

- **Mods** — jars installed into `mods/` (Fabric/Forge) or `plugins/` (Paper);
  hidden for vanilla worlds (no loader). Multi-select per world.
- **Datapacks** — data-only zips installed into `world/datapacks/`; available
  for every server type. Multi-select per world.
- **Resource packs** — client-delivered, not stored in the world: the server
  advertises the pack URL + sha1 (via the itzg `RESOURCE_PACK` env). One per
  world, with an optional "require" toggle.

How it works:

- **Source of truth is [Modrinth](https://modrinth.com)** (keyless read API).
  An admin adds an entry by **project slug** (e.g. `fabric-api`) on the Admin →
  *Add-on catalog* page; the backend fetches the project, validates its type
  matches the kind, and caches its loaders/versions. A `PINNED` fallback lets an
  admin supply a direct URL + sha512 for content not on Modrinth.
- **No version matrix is maintained.** The catalog stores only the project; the
  concrete file is resolved *per world* at install time by loader + Minecraft
  version. Entries with no compatible version are hidden for that world.
- **Every download is sha512-verified** before it is placed in a world, size-capped
  per kind, and (optionally) stored in a content-addressed cache
  (`PLATFORM_CONTENT_CACHE_DIR`) to dedupe across worlds and survive Modrinth outages.
- **Dependencies are gated**: a mod whose required dependencies are not also in
  the catalog is refused with a clear message (an admin must add them first).
- **Server-side-only mods are badged.** Modrinth's client/server side flags are
  cached at add-time; a mod that needs no client-side install (client side
  optional/unsupported) shows a "server-side only" badge in the world picker, so
  it's easy to pick content that works with a vanilla client.
- **Managed content survives imports.** Installed files are tracked by sha512;
  the import prune (see below) keeps matching jars and re-installs anything else
  the world tracks, so a re-import can't silently drop a world's managed content.

Applying or removing content requires the world to be **OFF** (like imports) and
runs as a worker job. This closes the "arbitrary jar" hole: user uploads are
pruned to a bare-essential world (all unmanaged jars stripped), and the only
executable content a world can gain is what an admin curated.

## Layout

This directory is one of three in the repository:

- [`api/`](../api/) — control-plane API + worker
- [`web/`](../web/) — SPA
- `deploy/` — this directory

Inside it, everything that belongs to one game sits under `games/<game>/`: its
compose file (the game's edge router, behind a profile named for the game) and
the router's own source. The rest is shared: `docker-compose.yml` (core stack,
monitoring, egress guard), `edge/`, `monitoring/`, `host/` and `scripts/`.
