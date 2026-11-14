import { describe, expect, it } from "vitest";
import type { Readiness, WorldState } from "./types";
import { worldStatus, type WorldStatus } from "./worldStatus";

describe("worldStatus", () => {
  it.each<[WorldState, Readiness | undefined, WorldStatus]>([
    ["RUNNING", "READY", "ready"],
    ["RUNNING", "STARTING", "starting"],
    ["RUNNING", "UNRESPONSIVE", "unresponsive"],
    // Readiness not reported yet: the container is up, so say so.
    ["RUNNING", undefined, "ready"],
    ["SLEEPING", "NOT_RUNNING", "sleeping"],
    ["STOPPING", "READY", "stopping"],
    ["OFF", "NOT_RUNNING", "off"],
    ["DELETING", "NOT_RUNNING", "deleting"],
    ["ERROR", "NOT_RUNNING", "error"],
  ])("%s / %s -> %s", (state, readiness, expected) => {
    expect(worldStatus(state, readiness)).toBe(expected);
  });

  // Only RUNNING is refined: a stale readiness must not relabel a stopped world.
  it("ignores readiness when the world is not running", () => {
    expect(worldStatus("OFF", "STARTING")).toBe("off");
    expect(worldStatus("SLEEPING", "READY")).toBe("sleeping");
  });
});
