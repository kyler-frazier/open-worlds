import type { AccessLevel, Limits, Me, World } from "./types";

/**
 * Per-world permission predicates, mirroring the server's two axes: the
 * **ladder** (`access_level`), on which admins sit at the top, and **strict
 * ownership**, which admins fail. Gating owner-only actions on the ladder
 * would show admins buttons the API then refuses.
 *
 * Components ask these rather than comparing ids themselves.
 */

const RANK: Record<AccessLevel, number> = { OPERATOR: 1, MANAGER: 2, OWNER: 3 };

function atLeast(have: AccessLevel, need: AccessLevel): boolean {
  return RANK[have] >= RANK[need];
}

/** Settings: memory, version, player cap, description, join password. */
export const canManageSettings = (w: World) =>
  atLeast(w.access_level, "MANAGER");

/**
 * Backups, deletion, and anything else that spends or destroys what the owner
 * has. The ladder, so admins are included — they can already delete any world.
 */
export const canManageBackups = (w: World) => w.access_level === "OWNER";

/**
 * The owner specifically. Admins are excluded, matching `is_world_owner` on
 * the server: replacing a world's contents reads from the owner's Drive and
 * overwrites what they built, so it stays theirs whatever role you hold.
 */
export const isStrictOwner = (w: World, me: Me | null) =>
  !!me && w.owner_id === me.id;

/**
 * "Allow unknown network connections" on a guarded world. Who may is the
 * operator's call (PLATFORM_EGRESS_OVERRIDE_ACTORS): nobody by default, admins only,
 * or anyone who manages the world. Operators never. Mirrors
 * `_require_egress_override` on the server.
 */
export function canChangeEgress(
  w: World,
  me: Me | null,
  limits: Limits | null,
): boolean {
  if (!w.egress_guarded || !limits || !me) return false;
  if (limits.egress_override_actors === "admin") return me.is_admin;
  if (limits.egress_override_actors === "owner_and_manager")
    return atLeast(w.access_level, "MANAGER");
  return false;
}
