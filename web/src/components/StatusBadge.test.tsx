import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Readiness, WorldState } from "../types";
import StatusBadge from "./StatusBadge";

describe("StatusBadge", () => {
  it.each<[WorldState, Readiness | undefined, string]>([
    ["RUNNING", "READY", "Running"],
    ["RUNNING", "STARTING", "Starting…"],
    ["RUNNING", "UNRESPONSIVE", "Not responding"],
    ["SLEEPING", undefined, "Sleeping"],
    ["STOPPING", undefined, "Stopping…"],
    ["OFF", undefined, "Off"],
    ["ERROR", undefined, "Error"],
    ["DELETING", undefined, "Deleting"],
  ])("%s / %s reads %s", (state, readiness, label) => {
    render(<StatusBadge state={state} readiness={readiness} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  // The first join fails while a server boots, so it must not read Running.
  it("does not call a booting world Running", () => {
    render(<StatusBadge state="RUNNING" readiness="STARTING" />);
    expect(screen.queryByText("Running")).not.toBeInTheDocument();
  });
});
