# Security

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue: use **Report a
vulnerability** on this repository's Security tab (GitHub private vulnerability
reporting). Include what you found, how to reproduce it, and what an attacker
gains.

Expect an acknowledgement within a week. Fixes land as normal commits once a
fix is ready; the report stays private until then.

## Scope

In scope: the api, worker and world manager (`api/`), the web UI (`web/`), and
the stack in `deploy/`: the edge, the routers, the egress guard and the host
configuration it installs.

Especially interesting:

- anything that lets a signed-in user act on a world they don't own, or a
  pending user act at all;
- anything that lets code in a world container reach the Docker daemon, the
  control plane's secrets, another world, or the host;
- ways around the egress guard or the upload sanitizing.

Out of scope: vulnerabilities in the game servers themselves (report those
upstream), and anything that needs an admin account or shell access to the
host.

The design and its accepted residual risks are described under "Security
posture" in [`deploy/README.md`](deploy/README.md#security-posture).
