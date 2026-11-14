# Socket proxy configuration

`haproxy.cfg.template` is the stock template from
`tecnativa/docker-socket-proxy:0.3.0` with blocks added, marked
`Local addition`. The image's entrypoint substitutes `${BIND_CONFIG}` into this
file and runs the result, so mounting it over the original is all that is
needed.

## What the addition does

Upstream authorises per **endpoint**, never per **object**. `CONTAINERS=1` plus
`POST=1` is the grant the platform needs to create, start, stop and remove
worlds -- and the same grant lets a caller stop or delete anything else on the
host: the database, the edge, this proxy. Nothing in a path-matching filter
distinguishes one container from another.

Platform worlds are named `game-world-<subdomain>`, so the *name* in the path
does distinguish them. The added rule denies any non-GET request that targets a
container whose path is not a `game-world-*` name, with `/containers/create`
excepted because it names nothing yet.

For that to hold, the orchestrator addresses containers by name rather than id
when it mutates them (`common/orchestrator.py`). An id in the path carries no
permission to mutate.

**Whose compromise this now guards against has changed.** The rule was written
when the api held the Docker credential. It no longer does -- `world-manager`
does, and api, worker and internal-api are not on this network at all. So this
is the second layer: if `world-manager` itself is compromised, it still cannot
stop the database, the edge, or this proxy. The first layer is that the process
most likely to be compromised cannot reach Docker in the first place.

Reads are untouched. Inspect, list and events still work on every container,
which is what mc-router's discovery needs.

## What a create may ask for

The name rule alone let a create under a `game-world-*` name ask Docker for
anything: privileged, the host's namespaces, `/` bind-mounted. The template
now buffers the create body (at most 48 KiB, no chunked or compressed bodies)
and refuses one that asks for:

- an image other than the two game servers by digest, or `GUARD_IMAGE`;
- a bind outside `WORLDS_ROOT`, or one with `..`;
- a network other than `GAMES_NETWORK` or `container:game-world-*`;
- a capability outside the list worlds and guards are given;
- privileged, host pid/ipc/uts/userns/cgroupns, a runtime, a cgroup parent,
  mounts, devices, `VolumesFrom`, masked/readonly path overrides, or an
  unconfined/seccomp/apparmor/label security option.

Docker reads the body's keys case-insensitively, lets a later duplicate win
and folds a few escapes and Unicode letters to ASCII, so every rule searches
the whole body ignoring case, and a body with those escapes is refused
outright. The world-manager makes the same checks before it sends
(`api/src/open_worlds/common/docker_policy.py`), so an orchestrator bug shows
up as an error naming the setting rather than a bare 403.

Networks are read-only, and the only image write is a pull of one of the two
game repos.

`WORLDS_ROOT`, `GAMES_NETWORK` and `GUARD_IMAGE` come from the compose file and
are spliced into regexes, so they must match what world-manager sends.

This is pattern matching on JSON, not parsing it: it holds because it refuses
anything it cannot read plainly, not because it understands every encoding.
The lasting fix is still a proxy that parses the request.

## What it does not do

It is a **name** check, not an ownership check. haproxy cannot resolve an id or
read a container's labels, so a container that someone else names
`game-world-*` would satisfy it. Closing that needs a proxy that can look the
container up and check the label the platform stamps.

## Why mc-router has its own proxy

mc-router addresses containers **by id**, verified from its own traffic:

```
POST /v1.43/containers/bac24f2d166ef1e...87287534/stop?t=60
```

The app proxy's name rule refuses that, so pointing mc-router at it would break
Minecraft wake-on-connect. Constraining *which* container it may touch needs a
proxy that can resolve an id to its labels.

What it can do is constrained by **verb** instead. Its entire observed API
surface is five shapes -- inspect, list, `HEAD /_ping`, the event stream, and
`POST .../stop` (plus `.../start` on wake). It never creates, deletes or pulls.

Upstream evaluates its blanket non-GET deny *before* the `ALLOW_START` /
`ALLOW_STOP` rules, so those only take effect when `POST` is already on -- and
`POST` also grants create, delete and update. `haproxy.router.cfg.template` is
the stock template with those three allows moved above the deny, which lets
`POST: 0` stand while start and stop still pass.

Net effect: full control of every container on the host becomes start-or-stop
of a container.

| | before | after |
|---|---|---|
| `POST /containers/create` | allowed | **403** |
| `DELETE /containers/{id}` | allowed | **403** |
| `POST /containers/{id}/kill` | allowed | **403** |
| `POST /images/create` | allowed | **403** |
| `POST /containers/{id}/start` | allowed | allowed |
| `POST /containers/{id}/stop` | allowed | allowed |
| reads, `/events` | allowed | allowed |

Verified end to end after the change: a join to a sleeping world produced
`POST /containers/<id>/start` → 204, the container started, and mc-router
relayed the connection.

## Verifying

```bash
P=http://docker-socket-proxy:2375/v1.43
run(){ docker run --rm --network open-worlds_socketproxy \
        curlimages/curl:latest -s -o /dev/null -w "%{http_code}" "$@"; }

run -X POST   "$P/containers/open-worlds-db-1/stop"   # 403
run -X DELETE "$P/containers/open-worlds-edge-1"      # 403
run -X POST   "$P/containers/game-world-demo/stop"             # allowed (404 if absent)
run           "$P/containers/json?all=1"                       # 200
```
