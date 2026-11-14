# Operations runbook

Day-to-day operation of Open Worlds on its host. For architecture,
security posture, DNS, OAuth, and the exposure map, see [README](README.md).

All commands run from `deploy/` in the checkout unless noted, with `.env` in
`deploy/`. The stack is a single Docker Compose project; `make` targets wrap
the common commands (see `make help`).

**One stack per Docker host.** Two copies of this stack on one daemon tell
their world containers apart only by a shared label, so each one's worker
removes the other's worlds as orphans. Develop and test elsewhere.

## The host

Any Linux machine or VM with **systemd and Docker Engine** from Docker's own apt
repository. Docker Desktop is not supported: its VM's clock steps backwards.
Known good: Ubuntu 26.04 LTS with Docker Engine 29.

How it starts from cold: `docker.service` is enabled, so the daemon starts at
boot, and every compose service is `restart: unless-stopped`. World containers
are not. They come back when a player connects (Palworld via pal-waker,
Minecraft via the placeholder behind mc-router) or when an owner turns one on.

What compose cannot set lives in [`host/`](host/), installed and checked by
`scripts/host-config.sh` (also `make host-config`, and run by `make verify`):

- **`host/docker/daemon.json`** goes to `/etc/docker/daemon.json`:
  user-namespace remapping and log rotation. `sudo scripts/host-config.sh --apply`
  installs it; read *Container UID remapping* below first on a host that did
  not already have it.
- **`host/ufw/after.rules.docker-user`** is the firewall block for published
  ports and container egress, which bypass ufw's own rules. It is a template:
  `HOST_IFACE`, `EDGE_PORT`, `EDGE_ALLOW_FROM` and `PLATFORM_GAMES_SUBNET` in
  `.env` fill it in, so the edge port answers only the outer reverse proxy and
  nothing on the games network opens a connection to a private network (DNS
  excepted). It backs up each world's guard, which refuses private addresses in
  both modes. Print the rendered block with
  `scripts/host-config.sh --print-firewall`, paste it into
  `/etc/ufw/after.rules`, and `sudo ufw reload`. The check compares the
  installed block with the rendered one; it is never applied automatically,
  because editing a live firewall belongs to a person.

Beyond that block, ufw denies incoming by default and allows only SSH from the
LAN. The game ports (25565/tcp, and the Palworld UDP range) are published by
compose and reach the host through the router's port forwards; see *Game
ports and pal-waker*.

```bash
docker info --format '{{.OperatingSystem}} {{.ServerVersion}}'
sudo systemctl restart docker   # restarts every container; make shutdown first if a world is running
journalctl -u docker -f         # daemon log
systemctl --failed              # should be empty
```

**Clock.** Any NTP client that slews rather than steps is fine
(`systemd-timesyncd` by default). Palworld treats a backwards frame on an empty
server as fatal; `PLATFORM_PALWORLD_ALLOW_NEGATIVE_DELTA_TIME` papers over a
host whose clock steps back, and a normal Linux host does not need it. To check
a host, hop across every CPU for a few minutes and count backwards steps; there
should be none:

```bash
python3 -I -c '
import os, time
cpus, last, back, end = sorted(os.sched_getaffinity(0)), 0.0, 0, time.monotonic() + 180
while time.monotonic() < end:
    for c in cpus:
        os.sched_setaffinity(0, {c}); t = time.monotonic(); back += t < last; last = t
print("backwards steps:", back)'
```

## On-disk layout

Two places matter: the **checkout** (this repository, with the stack's secrets
in `deploy/.env`, mode 600) and the **data root**, `PLATFORM_DATA_ROOT`
(compose default `/srv/games`; any path on a local filesystem works). The data
root is owned by the **remapped** range (`base + PUID`, see *Container UID
remapping*), not by the login user.

Checkout:

| Path | What it is |
|------|------------|
| `api/` | control-plane API, worker and world manager; build context for their image |
| `web/` | SPA; build context for the `web` image |
| `deploy/` | compose, `.env`, edge nginx, monitoring, routers, host config |

Data root:

| Path | What it is | Notes |
|------|------------|-------|
| `worlds/<subdomain>/data/` | a world's game data | bind-mounted into its container; the world's real state. World dirs are `0700` |
| `backup-staging/` | transient archive staging | uploads and imports land here mid-flight; swept by the worker |
| `platform-config/settings.json` | admin-tunable runtime config | read through a 30s TTL cache (see below) |
| `content-cache/` | sha512 content-addressed download cache | curated mods and datapacks; safe to delete, refills on demand |
| `observations/` | dns-observer report | the names worlds looked up and what guards refused; see *Egress guard* |

To relocate the data root, `make shutdown`, move the directory on the same
filesystem (or copy it with `cp -a` as root), set `PLATFORM_DATA_ROOT`, and
`make up`. Ownership carries over with the move; a copy made without `-a`
needs `sudo scripts/shift-owner.py --to-remapped`.

Postgres, VictoriaMetrics and Grafana keep their data in Docker volumes
(`<project>_pgdata18`, `_vmdata`, `_grafanadata`), not here. World containers
are named `game-world-<subdomain>` and carry the `open-worlds.*` labels; the
platform only ever touches containers with those labels.

## Legacy names

The project was once called Game Platform. Two of its old names are still read
on purpose, because archives and Drives from before the rename carry them:

- the Drive folder `Game Platform Backups`, which `DriveClient.app_root` renames
  in place to `Open Worlds Backups` (file ids are unchanged);
- the Palworld icon `Pal/Saved/gameplatform-icon.png`, which imports of older
  archives move to `open-worlds-icon.png` (`adopt_legacy_icons`).

## Container UID remapping (userns-remap)

The Docker daemon runs with **user-namespace remapping**, so uid N inside a
container is uid `base + N` on the host. World containers run mod code nobody
here wrote; if one escaped, its root would be an unprivileged host uid that
owns nothing but world data, not real root or the login user.

It is a daemon setting, so compose cannot turn it on. The config is kept in
[`host/docker/daemon.json`](host/docker/daemon.json), and three things check it:
`scripts/host-config.sh` (also `make host-config`), `make verify`, and
preflight, which refuses `make up` without it. The docs once said remapping
was on when it never had been, because nothing checked; now three things do.

- **Base**: the `dockremap` line in `/etc/subuid` and `/etc/subgid`, for
  example `165536`. With `PUID:PGID` 1000:1000, container-written data is then
  owned by `166536:166536` on the host, and container root is `165536`. Docker
  may assign `100000`, overlapping the login user's own sub-id range; move it
  by hand if so. `host-config.sh` flags any overlap.
- **Host access to world files**: they are no longer the login user's. Use `sudo`, or
  go through a container (`docker run --rm -u 1000:1000 -v <path>:/data alpine ...`).
- **Copying data in** (a restore, a legacy import) means shifting its owners:
  `sudo scripts/shift-owner.py --to-remapped <tree>` adds the base to every
  uid/gid and refuses a tree that is already shifted. `--to-host` reverses it.
- **Host-namespace services opt out** with `userns_mode: "host"`:
  `docker-socket-proxy` (reads the host socket) and `cadvisor` (reads host
  `/sys` and `/var/lib/docker`). Both are trusted images. So does `make scan`'s
  Trivy container, which reads images through the host socket. The egress
  guards stay remapped: `NET_ADMIN` works on their own network namespace
  without it.
- **Storage root**: remapped Docker keeps everything under
  `/var/lib/docker/165536.165536/` and uses the overlay2 driver, not the
  containerd image store. Turning remapping on or off switches roots, so
  images, containers, networks and volumes seem to vanish (they are still in
  the other root).

### Turning it on (or rebuilding a host)

About ten minutes of downtime on a host that already has data:

1. Dump the database, then `make shutdown`. Archive the volumes and the data
   root with `tar --numeric-owner` into a backup directory outside the checkout.
2. `sudo scripts/host-config.sh --apply`. Check `/etc/subuid` for an overlap
   (`scripts/host-config.sh` says), fix it, and `sudo systemctl restart docker`.
3. **Delete stale bridges.** The old root's external `games` network leaves its
   `br-<id>` interface behind with the same subnet, and it captures the route:
   world containers get DNS but no TCP. Compare `ip -br link show type bridge`
   with `docker network ls`, then `sudo ip link delete br-<old id>` and remove
   its leftover `MASQUERADE` rule (`sudo iptables-save | grep <old id>`).
4. `make net`.
5. `sudo scripts/shift-owner.py --to-remapped "$PLATFORM_DATA_ROOT"`.
6. For each of `pgdata18`, `vmdata` and `grafanadata`:
   - `docker volume create --label com.docker.compose.project=<project> --label com.docker.compose.volume=<v> <project>_<v>`, where `<project>` is compose's `name:`
   - `sudo cp -a /var/lib/docker/volumes/<project>_<v>/_data/. /var/lib/docker/165536.165536/volumes/<project>_<v>/_data/`
   - `sudo scripts/shift-owner.py --to-remapped` on the new `_data`.
7. `docker compose --profile guard build`, `make up`, `make verify`, then
   compare row counts with the dump.
8. Worlds that were SLEEPING come back OFF (their containers are gone). Start
   them once and put them back to sleep.

**Rollback**: put back the old `daemon.json` (in the backup dir), restart
Docker, `sudo scripts/shift-owner.py --to-host "$PLATFORM_DATA_ROOT"`, delete
the remapped root's stale `games` bridge as in step 3, and `make up`. The old
root's volumes are untouched, so nothing needs restoring. Preflight refuses
to start without remapping; skip `make up`'s preflight by running
`docker compose up -d` directly.

## Everyday commands

```bash
make ps                       # service status
make logs                     # tail all services
docker compose logs -f api    # one service (api|worker|internal-api|web|edge|mc-router|db)
make down                     # stop the core stack
make up                       # start it (also runs `make net` first)
```

### Checking the running stack still matches the repo

```bash
make verify
```

`docker compose up -d` is not a full reconcile, and the gaps are quiet:

- **A network keeps the IPAM it was created with.** Ours was created before the
  subnet was declared, so `172.29.0.0/16` was never applied and mc-router held a
  static IP that did not exist on that network -- for eight days, until a
  container was recreated and failed to start.
- **A mounted config file changes without the service definition changing**, so
  compose sees nothing to do. VictoriaMetrics kept scraping the old target list
  after `scrape.yml` was edited. Note that comparing file *contents* cannot find
  this: a bind mount is live, so the file inside the container is the file on
  disk. What is stale is the process, which read it once at startup -- so the
  check compares the file's mtime against the container's start time.
- **A container keeps the service definition it was created from.**

None of these surface as an error; they surface as behaviour that does not match
the file you are reading. `make verify` names the service, the file, and the
command that fixes it, and exits non-zero so it can gate a deploy.

### Redeploy after a code change

`api`, `internal-api`, `worker` and `world-manager` share one image
(`open-worlds-api:latest`); rebuild once and restart all four. Missing
`world-manager` is the easy mistake: it runs the orchestrator, so container
template changes do nothing until it restarts.

```bash
docker compose build api
docker compose up -d api internal-api worker world-manager   # api runs `alembic upgrade head` on start
```

Frontend only:

```bash
docker compose build web
docker compose up -d web
```

Edge nginx config only (`edge/nginx.conf`) — no rebuild needed:

```bash
docker compose restart edge
```

After starting `api`, it runs migrations before serving, so `/api/healthz`
returns `502` through the proxy for a few seconds during boot. Verify:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/api/healthz
```

## Shutting the host down without losing a world

Palworld saves on SIGTERM and needs up to 300 seconds to finish. Docker's
default is to wait **10**, then SIGKILL -- mid-save. Two things guard against
that, and they cover different situations.

**A deliberate shutdown: `make shutdown`.** Stops every running world with the
window its game declares, then brings the stack down. Use it before rebooting
or doing host maintenance.

```bash
make shutdown          # worlds first, then the stack
./scripts/graceful-shutdown.sh   # worlds only, leave the stack up
```

Note that plain `make down` does **not** stop worlds. World containers are
created by the orchestrator, not by docker-compose, so they are not part of the
compose project and `compose down` leaves them running.

The script does not touch world state in the database. The maintenance sweep
reconciles a world whose container vanished on the next boot; saving the world
is the part that cannot be redone later.

**An unattended reboot: `shutdown-timeout` in daemon.json.** When the daemon
stops on its own, it stops containers itself, and how long it waits is a daemon
setting rather than anything the platform controls:

```json
{ "shutdown-timeout": 300 }
```

That would go in [`host/docker/daemon.json`](host/docker/daemon.json), applied with
`sudo scripts/host-config.sh --apply`.

**Not set by default.** It is a daemon-wide setting that affects
every container on the machine, not just this project's, so it is left as a
deliberate choice rather than something the platform turns on. `make shutdown`
covers the deliberate reboot either way; this is only for the unattended one.

**Each container declares its own window.** World containers carry
`StopTimeout`, so a bare `docker stop` -- or `compose down`, or the daemon going
down -- gives the world its full graceful window without the caller having to
know. Verified on a live Palworld world: `docker stop` with no `-t` takes about
5 seconds and exits 0.

That required building containers through the low-level
`APIClient.create_container`, because the high-level `containers.create()`
refuses `stop_timeout`. The cost is that Docker's split between the container
config and the HostConfig is ours to maintain: `common/orchestrator.py` names
which half each setting belongs to, and **an unrouted setting raises** rather
than being silently dropped. `tests/test_container_equivalence.py` builds the
same container both ways and diffs it against Docker, so the two paths cannot
quietly diverge.

If you add a container setting and the API refuses to start with "is not routed
to the container config or the host config", that is this guard working: put
the key in whichever set matches Docker's own API.

## Configuration: two layers

1. **Static env** — `.env` (see [`.env.example`](.env.example)). Secrets, DB
   creds, domains, ports, `PUID/PGID`, caps like `PLATFORM_MAX_WORLDS_PER_USER`.
   Changing these requires `docker compose up -d` on the affected service.
   App services do **not** load `.env` wholesale: `docker-compose.yml` passes
   non-secret `PLATFORM_*` settings through its `x-platform-config` block and
   hands each service only the secrets it uses. A new `PLATFORM_*` setting must
   therefore be added to that block to take effect; `make preflight` refuses to
   start if `.env` contains one that compose would ignore. Preflight also boots
   each app service's config in its own container, so a service that would
   crash-loop on a missing secret or an unparseable value fails the deploy
   instead (set `PREFLIGHT_SKIP_CONFIG_CHECK=1` to skip that step).
   `.env` holds the DB password, session secret, Fernet key, and OAuth secret,
   so it **must be owner-only** (`chmod 600 .env`). `make up`
   runs a `preflight` check that refuses to start if `.env` is missing or more
   permissive than `600`.
2. **Runtime config** — `platform-config/settings.json`, edited by admins in the
   web UI (Admin screen). Holds `max_upload_bytes`, `max_upload_file_count`,
   `prune_garbage_enabled`, `extra_allowed_root_names`. Read through a 30s TTL
   cache, so edits take effect within ~30s with **no restart**. Missing file =
   built-in defaults.

## Admin bootstrapping

Google `sub` is the stable per-user id. Sign in once; the pending screen shows
your `sub`. Put it in `PLATFORM_ADMIN_GOOGLE_SUBS` in `.env`, then:

```bash
docker compose up -d api
```

On your next login you're seeded as admin and can approve other users from the
Admin screen. Unapproved sign-ins create no database rows (closed garden).

### Revoking a user

Revoke removes the allow-list row, and a revoked user's next sign-in lands on
the pending screen again. A user who still **owns worlds** cannot be revoked
blind: the Admin screen lists their worlds and offers three ways out --

- **Cancel.**
- **Delete their worlds, then revoke.** Each world goes through the same
  teardown as the owner's Delete button (container removed, data dir removed,
  Palworld port released), worlds first, then the user. If a teardown fails the
  user is *not* revoked and the remaining worlds stay put, so the admin can fix
  the cause and retry.
- **Back up to their Drive first, then delete and revoke.** Only offered when
  the user has Drive linked. Backups run under the user's own token into the
  user's own Drive; the admin never sees the archive and gets no download. The
  admin's browser waits for every backup to reach `SUCCESS` before deleting
  anything, and stops on the first failure with nothing deleted.

The worker's sweep also removes any labelled container that has no world row
(`removed orphan container` in the logs) -- the state a blind revoke used to
leave behind. It only removes the container; whatever is on disk is left for
you to inspect.

## World state vs. what's on the host

| DB state | Container | Data on disk |
|----------|-----------|--------------|
| `RUNNING` | `game-world-<sub>` running | `worlds/<sub>/data` |
| `SLEEPING` | exited, still present (mc-router auto-stopped it) | unchanged |
| `STOPPING` | running, being shut down gracefully | unchanged; resolves to `OFF` or `SLEEPING` (`stop_target`) when the container exits |
| `OFF` | removed (owner stopped it) | unchanged |
| `DELETING` | transient | being torn down; the sweep finishes it if wedged |
| `ERROR` | last action failed | inspect logs |

Inspect a specific world's container:

```bash
docker ps -a --filter name=game-world-<subdomain>
docker logs game-world-<subdomain>
```

List every managed world container:

```bash
docker ps -a --filter label=open-worlds=true
```

## The worker's self-healing sweep

The worker runs a maintenance pass every ~120s (and once on startup). It:

- **reaps stuck jobs** left `RUNNING` by a crash — requeues, or fails after
  `job_max_attempts` (stuck threshold: 30m);
- **finishes abandoned stops** — worlds left `STOPPING` for >10m (an API restart
  mid-shutdown) are stopped again toward their recorded target and resolved;
- **finishes wedged deletes** — worlds stuck in `DELETING` for >5m get their
  container + data dir removed and the row dropped (retries next pass if data
  removal fails, so it never orphans files);
- **sweeps abandoned staged uploads** older than 1h (row + staged file);
- **removes orphan containers** -- managed containers whose world row is gone
  (container only; data on disk is left alone);
- **reconciles world state** to the live containers and updates the per-state
  Prometheus gauge, counting unexpected (non-graceful) exits. A container that
  exited with a **non-graceful code** (anything but `0`/`143`) surfaces as
  **`ERROR`**, not `SLEEPING`, so a crash-looping world (e.g. a mod missing a
  hard dependency) reads as broken instead of a sleep it never wakes from.

So most transient wedges self-heal within a couple of minutes; check
`docker compose logs -f worker` to watch it.

## Backups and restore

Backups are per-world `.tar.gz` archives written to the **owner's Google Drive**
(scope `drive.file`), under an `Open Worlds Backups/<GAME>/<subdomain>/` folder. They
exclude the regenerable game install (server jar and loader caches for
Minecraft, the SteamCMD tree for Palworld) plus logs and caches, to stay small.
There are two kinds: `MANUAL` (kept until deleted) and `AUTO` (pruned to the
world's retention count). Both run through the worker's single job queue; the
world's save is flushed through its admin interface first when it's running.

A failing auto-backup schedule **backs off** rather than retrying every tick:
the retry delay doubles per consecutive failure from 15 minutes up to a day, and
resets on the next success. Only the five newest `FAILED` rows per world are
kept. When Google reports the user's refresh token as revoked (`invalid_grant`),
the worker **unlinks Drive** on that user and the dashboard tells them to
re-link; the schedule resumes once they do.

Older folder names (`Game Platform Backups`) are renamed in place on first use,
so file ids, and the backup rows pointing at them, are unchanged. Archives from
before the per-game layout sit directly under the app folder and are still
listed, without a game. Backups from an earlier folder still
(`Palworld Platform Backups`) restore by their stored file id but are not listed;
leave that folder alone unless its owner asks.

- **Create / list / delete**: from the web UI per world.
- **Restore**: in the UI, edit the world → "Replace content" → choose a Drive
  backup. It stages, validates (garbage/size checks), and swaps the data in only
  after a successful extract, so a bad archive never destroys the live world.

There is no separate CLI restore path — restore goes through the same staged,
validated import flow as uploads.

## Migrating an existing/legacy Minecraft world onto the platform

Used to bring a standalone server (e.g. an old `docker`/bare world) under
management without data loss or name clashes:

1. In the web UI, **create** the world with the desired subdomain, type,
   version, and memory. Leave starting content empty. This makes an empty
   `worlds/<subdomain>/data/` and registers the DB row. The version must be a
   **specific version** matching the legacy world's data — a release (e.g.
   `1.21.4`) or a specific snapshot (e.g. `24w14a`); only the rolling
   `LATEST`/`SNAPSHOT` aliases are rejected. The form defaults to the latest
   stable release — change it to match the world you're importing.
2. Ensure the world is **OFF** in the UI (no container running on that data).
3. Copy the legacy world data into `worlds/<subdomain>/data/` — the world
   folders, `server.properties`, ops/whitelist, mods/plugins, configs, etc.
   (Do **not** copy a `data/` wrapper; copy its *contents*.)
4. Fix ownership so the container can read/write. Because the daemon remaps
   uids (see *Container UID remapping*), files must be owned by
   `base + PUID : base + PGID`, not by the login user:

   ```bash
   base=$(grep ^dockremap /etc/subuid | cut -d: -f2)
   sudo chown -R "$((base+1000)):$((base+1000))" "$PLATFORM_DATA_ROOT/worlds/<subdomain>"
   ```

5. **Turn on** the world in the UI and watch `docker logs game-world-<subdomain>`.

Because create force-clears any pre-existing directory for a subdomain, always
create *first*, then copy in step 3 (not the other way around).

## Database

```bash
# psql shell
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

# run migrations manually (the migrate one-shot also runs on every `make up`)
make migrate

# dump / restore the whole DB
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > mcp-db.sql
cat mcp-db.sql | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

`$POSTGRES_USER`/`$POSTGRES_DB` come from `.env`; run these with the env loaded
(e.g. `set -a; . ./.env; set +a`) or substitute the literal values.

Roles: `$POSTGRES_USER` is the superuser, held only by `db` and the `db-init`
one-shot. `db-init` runs `db/roles.sql` on every `make up`: it creates
`ow_owner` (owns the schema; the `migrate` one-shot connects as it) and
`ow_app` (rows only; api, worker and internal-api connect as it), sets their
passwords from `POSTGRES_OWNER_PASSWORD`/`POSTGRES_APP_PASSWORD`, and moves
anything the superuser still owns to `ow_owner`. To rotate a password, change
it in `.env` and `make up`. A restored dump made before the roles existed is
fixed the same way: `make up` reassigns it.

### Upgrading to a new Postgres major

A major version cannot open the previous major's data directory, so this is a
dump and restore onto a new volume, never a digest bump. Dependabot still
opens PRs for majors, but CI fails them ("Postgres major matches its data
volume") so they show up red and cannot merge. Done this way for 16 → 18:

1. Turn every world off, then `docker compose stop api internal-api worker world-manager`.
2. Dump: `pg_dumpall --globals-only`, `pg_dump -Fc`, and a plain `pg_dump --clean`
   as a readable fallback. Record per-table row counts.
3. `docker compose stop db`, and tar the old volume as a second fallback.
4. In compose, change the image and point `db` at a **new** `pgdata<major>` volume
   (CI checks the two agree). From 18 on,
   the image keeps data in `/var/lib/postgresql/<major>/docker`, so mount the
   volume at `/var/lib/postgresql`. Leave the old volume declared but unmounted.
5. `docker compose up -d db`. The image creates the role and database from
   `.env`. Then `pg_restore --single-transaction --exit-on-error --no-owner`.
6. Compare row counts, data and `pg_dump -s` against the old dump. `--no-owner`
   leaves `public` owned by `pg_database_owner`; `alter schema public owner to`
   the app user to match.
7. Start the four services and check `/api/healthz` and the logs.

To roll back, revert the compose change: the old volume is untouched.

## Rotating keys

The session secret and the Fernet key can each be replaced without signing
anyone out or losing a stored secret. Save the new value in your password
manager first.

Fernet key (encrypts Drive refresh tokens and world passwords):

1. In `.env`, move the current `PLATFORM_FERNET_KEY` into
   `PLATFORM_FERNET_OLD_KEYS` and set a new `PLATFORM_FERNET_KEY`
   (generate as in `.env.example`). `make up`.
2. `docker compose run --rm worker platform-rotate-keys` re-encrypts every
   stored value with the new key. Safe to run again.
3. Clear `PLATFORM_FERNET_OLD_KEYS` and `make up`.

Session secret: move the current `PLATFORM_SESSION_SECRET` into
`PLATFORM_SESSION_OLD_SECRETS`, set a new one and `make up`. Cookies signed
with the old one keep working; new ones use the new. Clear the old value after
`session_max_age_seconds` (7 days). To end every session at once instead, just
replace the secret without listing the old one.

## Monitoring

The metrics stack starts with everything else (`make up`). Admins reach
Grafana from **Admin > Metrics**; access, and the SSH-tunnel break-glass for
when the api is down, are in the [README](README.md#accessing-grafana).

## Alerting

The alerts are provisioned from `monitoring/grafana/provisioning/alerting/`,
so they survive a rebuild of the Grafana volume and are reviewed in git.

| Alert | Fires when |
|---|---|
| A world exited unexpectedly | a managed container exited non-gracefully, or a Palworld world exited without the platform asking, in 15m |
| A worker job failed | a job finished `result=error` in 30m |
| Free disk for backups is low | under 10 GB free for 10 minutes |
| A platform service stopped reporting | `api`, `worker`, `world-manager` or `internal-api` unscraped for 5 minutes, or VictoriaMetrics not answering Grafana for 5 minutes |
| An egress guard refused something no list explains | a guard refused a name, port, protocol or flow that no DENY entry covers |
| DNS observer has stopped reporting | the dns-observer's report is stale, unreadable or unscraped for 10 minutes |

Each covers something nothing in the UI reports. They use `noDataState: OK`:
a counter nobody has incremented does not exist, and "nothing has failed" is
the healthy state. Absence is caught separately, on `up` and the reports' age.

### Changing or retiring an alert

Grafana reads provisioning only at startup, and `make up` leaves a running
Grafana alone, so an edited `rules.yaml` does nothing until Grafana restarts.
Deleting a rule from the file is not enough either: Grafana keeps the copy it
already has. To retire one, list its `uid` under `deleteRules` in `rules.yaml`,
then, after `make up`:

```bash
docker compose restart grafana
```

Check **Alerting > Alert rules** shows the set you expect. The `deleteRules`
entry can go once every Grafana that had the rule has restarted with it.

### Turning delivery on

Alerts go to **Discord**, via an incoming webhook. Grafana talks to Discord
through a webhook, not a bot -- there is no application, no token, no OAuth, and
nothing to keep logged in.

Create one in the channel you want the alerts in:

1. Discord → right-click the channel → **Edit Channel**
2. **Integrations** → **Webhooks** → **New Webhook**
3. Name it (this is what posts appear as) and optionally set an avatar
4. **Copy Webhook URL**

Then in `.env`:

```bash
ALERT_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
```

and `docker compose up -d --force-recreate grafana`.

**The URL is a credential, not an address.** Anyone holding it can post to that
channel as this integration. It stays in `.env`, never in git. To revoke, delete
the webhook in Discord and make a new one.

Grafana refuses to boot if the webhook URL is missing, so compose falls back
to an obviously fake placeholder and `make preflight` refuses to start while it
is still in place.

Test delivery without waiting for a real failure: Grafana UI → Alerting →
Contact points → `platform-discord` → **Test**.

### Checking them without the UI

```bash
PW=$(grep ^GRAFANA_ADMIN_PASSWORD .env | cut -d= -f2-)
curl -s -u "admin:$PW" localhost:3000/grafana/api/prometheus/grafana/api/v1/rules \
  | python3 -c "import sys,json
for g in json.load(sys.stdin)['data']['groups']:
    for r in g['rules']: print(r['name'], r['state'], r['health'])"
```

`health=ok` means the query ran. `health=error` usually means the rule points at
a datasource uid that does not exist -- the datasource is pinned to
`uid: victoriametrics` for exactly that reason.

## Curated content catalog

Admins curate installable **mods**, **datapacks**, and **resource packs**;
users pick from them on a world's Edit page.

- **Add an entry**: Admin page → *Add-on catalog* → pick the kind tab → enter a
  **Modrinth slug** (e.g. `fabric-api`) and *Add to catalog*. The backend fetches
  the project and rejects it if its type doesn't match the tab. For content not
  on Modrinth, switch the source to *Pinned* and supply name, loaders, game
  versions, download URL, and sha512 (sha1 too for resource packs).
- **Disable vs remove**: toggling *Enabled* hides an entry from users without
  deleting it; *Remove* deletes it (worlds keep already-installed files until a
  re-import strips them as unmanaged).
- **Installing**: on a world's Edit modal (world must be **OFF**), open the
  *Add-ons* tab, pick mods / datapacks from their multi-select dropdowns
  (the staged selection shows in a list below) or choose a resource pack, then
  hit **Save changes** — nothing is applied until you save (Cancel reverts the
  staging). Each add/remove enqueues a `CONTENT` worker job; the change appears a
  few seconds later once the job runs. Mods marked **server-side only** need no
  client-side install (they work with a vanilla client); the badge is derived
  from Modrinth's client/server side flags cached when the entry was added.
- **Where files land**: mods → `data/mods` (or `data/plugins` for Paper),
  datapacks → `data/world/datapacks`, resource packs → no file (the itzg
  `RESOURCE_PACK` env is set on next start).
- **Cache**: downloads are content-addressed under `PLATFORM_CONTENT_CACHE_DIR`
  (`<PLATFORM_DATA_ROOT>/content-cache`, mounted into the worker). Safe to delete to
  reclaim space; it re-populates on demand. Pre-create it owned by `PUID:PGID`.
- **Troubleshooting a stuck/failed apply**: `docker compose logs -f worker`. A
  failed job records the reason (e.g. "requires dependencies not in the catalog"
  or a sha512 mismatch) on the `jobs` row.

## Bumping a pinned image

Every third-party image is pinned by digest, tag included so the line stays
readable. The tag is a label; the digest is what Docker actually fetches, so a
rebuild gets the same bytes months later and a compromised or retagged upstream
cannot slip in silently.

That also means **no image ever updates on its own** -- including for security
fixes. Bumping is deliberate:

```bash
# Resolve the digest a tag currently points at
docker buildx imagetools inspect python:3.15-slim | awk '/^Digest:/{print $2; exit}'
```

Replace the `@sha256:...` after the tag, keeping the tag. Then `make build`,
`make up`, and confirm the service comes back. `make scan` (Trivy + pip-audit)
is what tells you a bump is due; base-image findings are cleared by bumping the
digest rather than by changing code.

Pinned in `docker-compose.yml`: postgres, both socket proxies, the edge nginx,
caddy, victoria-metrics, cadvisor, grafana; each game's `games/<game>/compose.yml`
pins its own (mc-router). Pinned in the Dockerfiles: the python bases of the api
and of the services under `deploy/`, the uv binary, the node builder and the
web nginx. Dependabot opens weekly PRs for all of them (`.github/dependabot.yml`),
except the uv binary; Postgres major PRs fail CI on purpose.

### The game server images

The Minecraft and Palworld images are pinned in Python, in each game's
`adapter.py`, so Dependabot cannot see them. `scripts/game-images.sh --check`
names a newer release (a version tag at least 14 days old; `MIN_AGE_DAYS`
overrides), and the weekly `game-images` workflow runs it with a Trivy summary
and keeps one open issue while a bump is due. To bump:

1. Read the image's release notes (both version their own releases, not the
   game's; Palworld's needs `SETUP_ONLY`, v2.8.1 or later).
2. Try an install on a development machine with the new digest, as the install stage runs it:
   for Minecraft, `TYPE` VANILLA (`SETUP_ONLY=true`), PAPER
   (`JVM_DD_OPTS=paperclip.patchonly:true`) and FABRIC (`EXTRA_ARGS=--help`),
   each with `EULA=TRUE` and a world's `VERSION`, into a fresh `/data` dir.
3. Change the digest in the adapter, run the api tests, deploy. Each world
   reinstalls on its next start (the install key includes the image).

## Security scanning

Run the vulnerability scan on demand (or from cron):

```bash
make scan
```

This runs two checks:

- **Trivy** against every image in `docker-compose.yml`, across all profiles,
  and the game server images (`scripts/game-images.sh`): the ones built here
  and the pinned third-party ones (HIGH/CRITICAL, fixable only). Findings in a third-party image clear only when upstream publishes a
  rebuild. Application/language-package findings should be
  zero; when Trivy reports **base-image OS packages** (Alpine `apk` entries such
  as `libxml2`, `musl`, `zlib`), the fix is to rebuild on a refreshed base
  (`make build`) — no code change resolves them. The scan intentionally exits
  non-zero on any fixable HIGH/CRITICAL so a scheduled job can gate on it.
- **`pip-audit`** against the API's locked Python dependency set (via
  `uv export --no-emit-project`, so only third-party deps are audited).

[Dependabot](../.github/dependabot.yml) opens weekly PRs for
Python/npm/Docker updates in `api/` and `web/`, and for the pinned compose
images here; merge those and rerun
`make build && make scan` to confirm.

## Egress guard (per-world outbound enforcement)

Every world runs as two containers: `game-world-<sub>--egress` owns the
network namespace and the world's name on the `games` network, and
`game-world-<sub>` joins it. With **blocking on**, a world can reach only what
its game's list for the current **stage** (`games/<game>/egress.py`, loaded and
checked by `egress-guard/trust.py`) names for its server variant.

**Stages.** A world boots in two steps, each behind its own list:

- **install** (`INSTALL`): a container that downloads the server and exits.
  Minecraft installs when its marker (`.open-worlds-install.json` in the data
  dir) is missing, names another version, loader or image, is over 7 days old,
  or its files are gone. `mods/` and `plugins/` are hidden from it (tmpfs), so
  a world's own code never runs with the install's access. Palworld installs
  when `PalServer.sh` or its Steam manifest is missing.
- **play** (`PLAY`): the server, from the files on disk (Minecraft as
  `TYPE=CUSTOM`). It can reach only what players need: authentication, and
  for Palworld, crossplay. No CDN, no maven, no Steam.

The install container uses the world's name. When it exits 0 the world reads
as still starting, and the worker's `install_handoff` job (every
`PLATFORM_READINESS_PROBE_INTERVAL_SECONDS`) starts the server, which replaces
the guard with a play-stage one. A failed install is a crash, with its log on
the world's card; the next start installs again. A running server is never
reinstalled under: an install that is due waits for the next start. To force
one, delete the marker and sleep/wake the world.

What each list allows:

- **Lookups:** only names some entry for the game lists are answered.
- **TCP, every port:** redirected into the guard's gate, which reads the
  connection's first bytes (TLS name, or every HTTP `Host`). An entry must match
  the name, protocol and port. The guard then connects to its own lookup of
  that name, ignoring the address the world dialled.
- **Everything else is rejected:** UDP (QUIC included), unlisted ports, and
  connections naming no site.

With **blocking off** it lets everything through and records what it would have
refused. Inbound traffic --
players via mc-router/pal-waker, RCON and REST from the worker -- is unaffected
either way.

Blocking is a per-world value, stored on the world:

- `PLATFORM_EGRESS_BLOCKING_DEFAULT` (default `false`) sets it for **new**
  worlds. Changing it leaves existing worlds alone.
- `PLATFORM_EGRESS_OVERRIDE_ACTORS` (default `none`) decides who may change it later in
  the world's settings (*Allow unknown network connections*): nobody, `admin`, or
  `owner_and_manager`. Each change is logged by the api as
  `egress override changed`, with who made it.
- `PLATFORM_EGRESS_GUARD_ENABLED` (default `true`) is the emergency switch:
  `false` removes every guard at each world's next start.

```bash
docker compose --profile guard build egress-guard        # once, and after any egress policy edit
docker compose up -d api internal-api worker world-manager   # after changing any of the above
```

A change to a world's blocking, or a rebuilt guard image, takes effect at the
world's next start (sleep/wake counts): its guard and container are replaced.

**Entries** (`games/<game>/egress.py`). Each entry in `INSTALL` or `PLAY` is
scoped as tightly as the game allows:

- **Games**, and **variants** for Minecraft (`VANILLA`, `PAPER`, `FABRIC`,
  `FORGE`; none listed means every variant).
- **Name:** exact, or `*.domain` for pools whose names rotate.
- **Protocol:** `tls` checks the TLS name, `http` checks every `Host` header.
  `stream` is TCP the gate can't read, allowed only to an address the world
  looked up as that name; there are none today.
- **Ports.**
- **Reason:** required. An empty or `TODO` reason fails the import, so the guard
  won't build, and fails the tests.

`DENY` entries carry games, name and reason only, plus `stages={...}` when a
host is refused in one stage only (Paper's build service: its download at
install, its update check while playing). They are refused anyway, and are
listed so the alert stays quiet for hosts we refuse on purpose. A `DENY` that
overlaps an allowed entry in the same stage fails the import. Steam's
connection servers on 27018-27038 are TLS websockets, so they get the same name
check as 443. Changing a world's server type replaces its guard at the next
start.

**Refusals.** Every refusal is one JSON line in the guard's log (`refused {...}`,
or `would refuse {...}` with blocking off), on the first occurrence and every
100th after. The first one is also reported to the dns-observer. Each record
carries:

- world, game, variant, **stage** and mode;
- **layer:**
  - `dns`: a lookup;
  - `gate`: a TCP connection;
  - `firewall`: UDP, or other non-TCP traffic;
- **reason:** `unlisted_name`, `denied`, `port_not_allowed`,
  `proto_not_allowed`, `no_name`, `ech`, `not_tcp`, `no_client_bytes` or
  `stream_addr_not_resolved`;
- the protocol, name, address and port;
- `names_for_dst`: the names this world looked that address up as;
- the TLS ALPN, and the first bytes of an unreadable stream;
- `suggest`: a ready-to-paste entry with reason `TODO`.

A refused *lookup* never reaches a connection, so its suggestion guesses
`tls`/443 and says so. Switch the world's blocking off to see the real protocol
and port.

```bash
scripts/egress-refusals.sh          # refusals no DENY entry explains, newest first
scripts/egress-refusals.sh --all    # including DENY hits
```

**Adding an entry:**
1. Paste the suggestion into the list for the record's stage (`INSTALL` or
   `PLAY`; `egress-refusals.sh` says which).
2. Replace `TODO` with why the game needs it.
3. Rebuild the guard (`docker compose --profile guard build egress-guard`).
4. Sleep and wake the world.

**Alerts:**
- *An egress guard refused something no list explains* fires on a new refusal
  that no `DENY` entry covers for the world's game, variant and stage. That includes an allowed
  name on a new port or protocol.
- *DNS observer has stopped reporting* fires if the dns-observer goes quiet.

ECH is refused, and HTTPS/SVCB lookups (which carry ECH keys) are answered
empty, so ordinary clients keep their TLS name readable. Installs go through the
gate too, behind their own list.

To count what the firewall dropped, and see the drops that are still current:

```bash
docker exec game-world-<sub>--egress nft list counter inet guard blocked
docker exec game-world-<sub>--egress nft list set inet guard refused4
```

**If a world will not start** with "image unavailable" in the worker log, the
detail names the guard: either the image is not built, or the guard failed its
self-test. Before it reports healthy, it checks that:
- the gate refuses an unapproved name, and an approved name on an unlisted port;
- its own upstream connections can't reach an unlisted port;
- a UDP drop is recorded.

A guard with no `GUARD_GAME` or `GUARD_STAGE` refuses to start. The world is left off rather than started without its fence.

**Debugging a blocked world**: switch its blocking off, play, and run
`scripts/egress-refusals.sh`. It names exactly what blocking would break, with
the real protocol and port of each.

**If a game breaks behind it**, the refusals are the first place to look:
something it needs is missing from its stage's list. Add the entry, rebuild,
and sleep/wake the world. Test an install (a new world, or one with its marker
deleted) as well as a restart: they run behind different lists.

## Game ports and pal-waker (Palworld only)

Every Palworld world owns one UDP port for its life; Minecraft worlds share
25565 and allocate nothing.

```bash
docker compose logs -f pal-waker     # binds, wakes, sleeps, relay errors
```

One-line JSON: `listening`, `waking world on connect`, `sleeping idle world`,
`releasing port`, `could not bind port`.

**Not every packet wakes a world.** The range is public, and the internet sends
lone UDP probes to every open port all day; one probe every ten minutes used to
keep an 8 GB server awake indefinitely. A wake now needs
`PLATFORM_WAKE_MIN_DATAGRAMS` datagrams from the *same* source within
`PLATFORM_WAKE_WINDOW_SECONDS` (a client retrying its handshake does this in
one connect attempt; a scanner sends one and moves on), and a port that just
woke its world will not do so again for `PLATFORM_WAKE_COOLDOWN_SECONDS`. If a
player's first attempt never wakes the world, lower the datagram count; if
`waking world on connect` shows up with nobody playing, raise it. The owner's
**Turn on** in the dashboard is unaffected by any of this.

**The range must be forwarded to this host, not just published on it.** Docker
publishing `8211-8220/udp` only gets a packet from the host to the container —
it does nothing about getting the packet to the host in the first place. Players
are on the internet, so the whole range needs:

- a **UDP** port-forward (or firewall allow) for `8211-8220` from the router or
  cloud security group to this host. UDP specifically: forwarding TCP does
  nothing here, and this is the single most common reason a world that looks
  perfectly healthy in the dashboard is unreachable.
- no per-world DNS work. `*.<PLATFORM_SERVERS_DOMAIN>` already resolves to
  this host; for Palworld the hostname is cosmetic and the **port** selects the world.

Unlike Minecraft — one forwarded port, 25565, set up once and never touched —
this range changes meaning as it grows. **Growing the range means forwarding the
new ports too.** A world allocated port 8221 after you widen the range is
reachable from inside the host and nowhere else until the forward covers it, and
nothing in the platform can detect that.

Verify from *outside* the network, not from the host (a local test passes even
with no forward at all):

```bash
# from another network; expect a response, not a timeout
nc -zvu <public-host> 8211
```

**Changing the range.** Edit `PLATFORM_GAME_PORT_RANGE_START`/`_END` in `.env`,
then `docker compose up -d pal-waker api worker internal-api`, then update the
router's port forward to match. Growing is safe; shrinking below a port already assigned
strands that world (it stays in the database but nothing listens on its port) —
delete or recreate those worlds first.

**Publishing a UDP range costs a `docker-proxy` process per port** under
Docker's defaults. Ten is unremarkable. Before growing it substantially, set
`{"userland-proxy": false}` in `/etc/docker/daemon.json` and restart Docker
(which restarts every container), and add the setting to
`host/docker/daemon.json` so `host-config.sh` keeps agreeing. Test a join from
outside before relying on it.

## Choosing which games are offered

Admins choose the games people can create worlds for under **Admin > Settings
> Games**. Minecraft is on by default. A game with worlds, in any state, stays
on until they are deleted, and at least one game always is.

A game can only be turned on while its router runs here. Each game's router
lives in `games/<game>/compose.yml` under a profile named for the game, and
`COMPOSE_PROFILES` in `.env` turns it on. So, to offer Palworld: add
`palworld` to `COMPOSE_PROFILES`, forward its UDP port range on the router,
`make up`, then tick it in the admin panel, which shows what is missing if
anything is. To stop running a router, untick the game first (only possible
once it has no worlds), drop its profile, and `docker compose rm -sf <service>`.

`PLATFORM_ENABLED_GAMES` is retired; preflight and the api refuse an `.env`
that still sets it.

## Troubleshooting

| Symptom | Where to look |
|---------|---------------|
| `/api/healthz` 502 right after deploy | api is running migrations; wait ~10s and retry |
| World stuck in `DELETING` | `docker compose logs -f worker`; the sweep resolves deletes >5m old |
| World stuck in `STOPPING` | normal for a few minutes (Palworld saves on shutdown). Past 10m the worker's sweep redoes the stop toward `stop_target`; if Docker refuses, the world lands in `Error` and the next reconcile reads the real container state |
| Backup stuck `PENDING`/`RUNNING` | worker queue is serial; check worker logs; a crashed job is reaped after 30m |
| Auto-backups keep failing | `error` on the `backups` row says why. "Drive access was revoked" means the user must re-link Drive (the worker already unlinked it); otherwise check Drive quota and worker logs. Retries back off up to once a day |
| Palworld player times out on first connect | expected: the world was asleep and this attempt woke it. The server needs ~30-60s; reconnect once. Confirm with a `waking world on connect` line in `docker compose logs pal-waker` |
| Palworld world unreachable entirely | its port is outside the published range — compare `PLATFORM_GAME_PORT_RANGE_*` with `docker compose port pal-waker` |
| Palworld world unreachable *from the internet* but fine on the LAN | the UDP port-forward doesn't cover its port. Check the router/security-group rule against `PLATFORM_GAME_PORT_RANGE_*`, and remember a widened range needs the forward widened too (see *Game ports and pal-waker*) |
| Palworld world in `Error` never wakes on connect | by design: pal-waker binds no port for an `Error` world, so joining cannot restart something that crashed. Fix the cause (`docker logs game-world-<subdomain>`), then press **Turn on** in the dashboard |
| World won't start, "server image is unavailable" | the image pin can't be pulled — bad digest, or no registry access. `docker pull` the adapter's pinned image by hand to see the real error; not a problem with the world |
| Can't create a world of some game | it is not offered: Admin > Settings > Games says why |
| Player sees "Can't connect" | subdomain has no world row (deleted/typo); OFF worlds instead show the offline-placeholder MOTD |
| World shows `Error` (was "won't start") | it crashed on boot (non-graceful exit); `docker logs game-world-<subdomain>` — usually a version/type mismatch, a mod missing a hard dependency (e.g. Fabric API), or bad imported data. Fix the cause, then start it again |
| Upload rejected as too large / garbage | adjust `platform-config/settings.json` via the Admin UI (caps, prune toggle, allow-list) |
| Import strips mods/configs the user wanted | expected: uploads are pruned to a bare-essential world (all jars + `kubejs/`/`scripts/` removed). Add legitimate top-level names to `extra_allowed_root_names`; jars must come from the managed add-on catalog, not uploads |
| Can't write to `worlds/<sub>` | ownership drift — `sudo chown -R 166536:166536` it, `base+PUID` (see *Container UID remapping*); preflight catches this for `worlds/` itself |
