import { describe, expect, it } from "vitest";
import { makeWorld } from "./test/fixtures";
import { sortWorlds, withUpdatedWorld } from "./worldList";

const ids = (ws: { id: string }[]) => ws.map((w) => w.id);

describe("withUpdatedWorld", () => {
  it("merges the update into the matching world only", () => {
    const a = makeWorld({ id: "a", owner_email: "a@example.com" });
    const b = makeWorld({ id: "b" });
    const updated = makeWorld({ id: "a", state: "RUNNING", owner_email: null });
    const out = withUpdatedWorld([a, b], { ...updated });
    expect(out?.[0].state).toBe("RUNNING");
    expect(out?.[1]).toBe(b);
  });

  it("keeps fields the single-world answer lacks", () => {
    const a = makeWorld({ id: "a", owner_email: "a@example.com" });
    const { owner_email: _drop, ...partial } = makeWorld({
      id: "a",
      state: "RUNNING",
    });
    const out = withUpdatedWorld([a], partial as typeof a);
    expect(out?.[0].owner_email).toBe("a@example.com");
  });

  it("passes null through", () => {
    expect(withUpdatedWorld(null, makeWorld())).toBeNull();
  });
});

describe("sortWorlds", () => {
  it("sorts by status, joinable first", () => {
    const worlds = [
      makeWorld({ id: "off", state: "OFF" }),
      makeWorld({ id: "err", state: "ERROR" }),
      makeWorld({ id: "ready", state: "RUNNING", readiness: "READY" }),
      makeWorld({ id: "boot", state: "RUNNING", readiness: "STARTING" }),
      makeWorld({ id: "zzz", state: "SLEEPING" }),
    ];
    expect(ids(sortWorlds(worlds, "status"))).toEqual([
      "ready",
      "boot",
      "zzz",
      "off",
      "err",
    ]);
  });

  it("sorts by name case-insensitively and numerically", () => {
    const worlds = [
      makeWorld({ id: "1", display_name: "world 10" }),
      makeWorld({ id: "2", display_name: "World 2" }),
      makeWorld({ id: "3", display_name: "apple" }),
    ];
    expect(ids(sortWorlds(worlds, "name"))).toEqual(["3", "2", "1"]);
  });

  it("puts the most recently played first and never-played last", () => {
    const worlds = [
      makeWorld({ id: "never", last_active_at: null }),
      makeWorld({ id: "old", last_active_at: "2026-01-01T00:00:00Z" }),
      makeWorld({ id: "new", last_active_at: "2026-06-01T00:00:00Z" }),
    ];
    expect(ids(sortWorlds(worlds, "last_played"))).toEqual([
      "new",
      "old",
      "never",
    ]);
  });

  it("breaks ties down the default chain, then by id", () => {
    // Same status: falls to last played, then name, then id.
    const worlds = [
      makeWorld({ id: "c", display_name: "Same" }),
      makeWorld({ id: "b", display_name: "Same" }),
      makeWorld({ id: "a", display_name: "Bravo" }),
      makeWorld({
        id: "d",
        display_name: "Zulu",
        last_active_at: "2026-01-01T00:00:00Z",
      }),
    ];
    expect(ids(sortWorlds(worlds, "status"))).toEqual(["d", "a", "b", "c"]);
  });

  it("does not mutate its input", () => {
    const worlds = [makeWorld({ id: "b" }), makeWorld({ id: "a" })];
    sortWorlds(worlds, "name");
    expect(ids(worlds)).toEqual(["b", "a"]);
  });
});
