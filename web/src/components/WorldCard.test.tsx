import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api";
import { makeMe, makeWorld } from "../test/fixtures";
import { renderSignedIn } from "../test/render";
import type { World } from "../types";
import WorldCard from "./WorldCard";

function renderCard(world: World, me = makeMe()) {
  const onUpdated = vi.fn();
  const onChanged = vi.fn();
  return {
    onUpdated,
    onChanged,
    ...renderSignedIn(
      <WorldCard
        world={world}
        driveLinked
        onChanged={onChanged}
        onUpdated={onUpdated}
      />,
      { me },
    ),
  };
}

const button = (name: string | RegExp) => screen.getByRole("button", { name });

describe("WorldCard", () => {
  it("turns an off world on and shows the answer at once", async () => {
    const running = makeWorld({ state: "RUNNING", readiness: "STARTING" });
    vi.spyOn(api, "startWorld").mockResolvedValue(running);
    const { onUpdated, onChanged } = renderCard(makeWorld({ state: "OFF" }));
    await userEvent.click(
      await screen.findByRole("button", { name: "Turn on" }),
    );
    expect(api.startWorld).toHaveBeenCalledWith("w1");
    expect(onUpdated).toHaveBeenCalledWith(running);
    expect(onChanged).toHaveBeenCalled();
  });

  it("offers Wake now and Turn off for a sleeping world", async () => {
    renderCard(makeWorld({ state: "SLEEPING" }));
    expect(
      await screen.findByRole("button", { name: "Wake now" }),
    ).toBeEnabled();
    expect(button("Turn off")).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  });

  it("locks every control while the world is stopping", async () => {
    renderCard(makeWorld({ state: "STOPPING" }));
    expect(
      await screen.findByRole("button", { name: "Stopping…" }),
    ).toBeDisabled();
    expect(button("Settings, backups and deletion")).toBeDisabled();
  });

  it("reads Stopping from the click, before the API answers", async () => {
    let answer!: (w: World) => void;
    vi.spyOn(api, "stopWorld").mockReturnValue(
      new Promise((r) => (answer = r)),
    );
    renderCard(makeWorld({ state: "RUNNING", readiness: "READY" }));
    await userEvent.click(
      await screen.findByRole("button", { name: "Turn off" }),
    );
    expect(button("Stopping…")).toBeDisabled();
    answer(makeWorld({ state: "OFF" }));
  });

  it("cannot be turned on while deleting", async () => {
    renderCard(makeWorld({ state: "DELETING" }));
    expect(
      await screen.findByRole("button", { name: "Turn on" }),
    ).toBeDisabled();
  });

  it("shows the API's refusal and unlocks again", async () => {
    vi.spyOn(api, "startWorld").mockRejectedValue(
      new ApiError(409, "No ports are free"),
    );
    renderCard(makeWorld({ state: "OFF" }));
    await userEvent.click(
      await screen.findByRole("button", { name: "Turn on" }),
    );
    expect(await screen.findByText("No ports are free")).toBeInTheDocument();
    expect(button("Turn on")).toBeEnabled();
  });

  it("marks a world shared with you, with your access level", async () => {
    const me = makeMe({ id: "u-friend" });
    renderCard(makeWorld({ access_level: "MANAGER" }), me);
    expect(await screen.findByText("shared · manager")).toBeInTheDocument();
    expect(screen.getByText("owner: owner@example.com")).toBeInTheDocument();
  });

  it("does not mark your own world shared", async () => {
    renderCard(makeWorld());
    await screen.findByRole("button", { name: "Turn on" });
    expect(screen.queryByText(/shared ·/)).toBeNull();
  });

  it("flags a world whose network fence is open", async () => {
    renderCard(makeWorld({ egress_open_exception: true }));
    expect(await screen.findByText("⚠️ Network Open")).toBeInTheDocument();
  });

  it("shows players online only while running", async () => {
    renderCard(
      makeWorld({ state: "RUNNING", readiness: "READY", player_count: 3 }),
    );
    expect(
      await screen.findByTitle("Players online / capacity"),
    ).toHaveTextContent("3/10 players");
  });
});
