import type { World } from "./types";
import { worldStatus, type WorldStatus } from "./worldStatus";

/** Swap one world in a list for the API's newer copy of it. Merged rather than
 * replaced: a list row can carry fields (the admin list's owner) that a
 * single-world answer does not. */
export function withUpdatedWorld(
  worlds: World[] | null,
  updated: World,
): World[] | null {
  return (
    worlds?.map((w) => (w.id === updated.id ? { ...w, ...updated } : w)) ?? null
  );
}

export type WorldSort = "last_played" | "name" | "last_started" | "status";

// Listed in the default tie-break order, which is derived from this list --
// so the dropdown reads as the chain it sorts by.
export const WORLD_SORTS: { value: WorldSort; label: string }[] = [
  { value: "status", label: "Status" },
  { value: "last_played", label: "Last played" },
  { value: "name", label: "Name" },
  { value: "last_started", label: "Last turned on" },
];

// Up and joinable first: the order a glance at the grid is asking about.
const STATUS_ORDER: WorldStatus[] = [
  "ready",
  "starting",
  "unresponsive",
  "stopping",
  "sleeping",
  "off",
  "error",
  "deleting",
];

const byName = (a: World, b: World) =>
  a.display_name.localeCompare(b.display_name, undefined, {
    sensitivity: "base",
    numeric: true,
  });

/** Most recent first; a world with no timestamp goes last. */
const byRecent =
  (field: "last_active_at" | "started_at") => (a: World, b: World) => {
    const ta = a[field] ? Date.parse(a[field]!) : -Infinity;
    const tb = b[field] ? Date.parse(b[field]!) : -Infinity;
    return tb - ta;
  };

const byStatus = (a: World, b: World) =>
  STATUS_ORDER.indexOf(worldStatus(a.state, a.readiness)) -
  STATUS_ORDER.indexOf(worldStatus(b.state, b.readiness));

const COMPARATORS: Record<WorldSort, (a: World, b: World) => number> = {
  last_played: byRecent("last_active_at"),
  status: byStatus,
  name: byName,
  last_started: byRecent("started_at"),
};

// The tie-break chain when nothing is chosen, most telling first.
const DEFAULT_CHAIN: WorldSort[] = WORLD_SORTS.map((s) => s.value);

/** The chosen key first, the rest in default order -- never in the order they
 * were last picked, so one choice always sorts the same way. */
function sortChain(sort: WorldSort): WorldSort[] {
  return [sort, ...DEFAULT_CHAIN.filter((k) => k !== sort)];
}

/** A sorted copy, compared key by key down the chain. Ends on id, so even
 * worlds identical on every key hold their places between polls. */
export function sortWorlds(worlds: World[], sort: WorldSort): World[] {
  const chain = sortChain(sort).map((k) => COMPARATORS[k]);
  return [...worlds].sort((a, b) => {
    for (const cmp of chain) {
      const d = cmp(a, b);
      if (d) return d;
    }
    return a.id.localeCompare(b.id);
  });
}
