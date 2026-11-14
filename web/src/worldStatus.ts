import type { Readiness, WorldState } from "./types";

/**
 * The world's status as the UI presents it: RUNNING refined by readiness, since
 * the container being up and the world being joinable are 30-60s+ apart.
 * Shared so the badge and the card tint cannot disagree.
 */
export type WorldStatus =
  | "ready"
  | "starting"
  | "unresponsive"
  | "sleeping"
  | "stopping"
  | "off"
  | "deleting"
  | "error";

export function worldStatus(
  state: WorldState,
  readiness?: Readiness,
): WorldStatus {
  if (state === "RUNNING") {
    if (readiness === "STARTING") return "starting";
    if (readiness === "UNRESPONSIVE") return "unresponsive";
    return "ready";
  }
  switch (state) {
    case "SLEEPING":
      return "sleeping";
    case "STOPPING":
      return "stopping";
    case "DELETING":
      return "deleting";
    case "ERROR":
      return "error";
    default:
      return "off";
  }
}
