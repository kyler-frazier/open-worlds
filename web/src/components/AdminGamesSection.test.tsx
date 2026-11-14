import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { AdminGame } from "../types";
import AdminGamesSection from "./AdminGamesSection";

const game = (over: Partial<AdminGame>): AdminGame => ({
  game: "MINECRAFT",
  display_name: "Minecraft",
  enabled: true,
  world_count: 0,
  problems: [],
  ...over,
});

async function renderWith(games: AdminGame[]) {
  vi.spyOn(api, "adminListGames").mockResolvedValue(games);
  render(<AdminGamesSection />);
  await screen.findByRole("button", { name: "Save games" });
}

const box = (name: string) =>
  screen.getByRole("checkbox", { name: new RegExp(name) });

describe("AdminGamesSection", () => {
  it("keeps a game with worlds on", async () => {
    await renderWith([
      game({ world_count: 2 }),
      game({ game: "PALWORLD", display_name: "Palworld" }),
    ]);
    expect(box("Minecraft")).toBeDisabled();
    expect(box("Palworld")).toBeEnabled();
    expect(
      screen.getByText("Stays on while it has worlds."),
    ).toBeInTheDocument();
  });

  it("keeps the last enabled game on", async () => {
    await renderWith([
      game({}),
      game({ game: "PALWORLD", display_name: "Palworld", enabled: false }),
    ]);
    expect(box("Minecraft")).toBeDisabled();
  });

  it("will not turn on a game with problems", async () => {
    await renderWith([
      game({}),
      game({
        game: "PALWORLD",
        display_name: "Palworld",
        enabled: false,
        problems: ["Its router is not running."],
      }),
    ]);
    expect(box("Palworld")).toBeDisabled();
    expect(screen.getByText("Its router is not running.")).toBeInTheDocument();
  });

  it("saves only once something changed, and sends the chosen set", async () => {
    const pal = game({
      game: "PALWORLD",
      display_name: "Palworld",
      enabled: false,
    });
    await renderWith([game({}), pal]);
    const save = screen.getByRole("button", { name: "Save games" });
    expect(save).toBeDisabled();

    vi.spyOn(api, "adminSetGames").mockResolvedValue([
      game({}),
      { ...pal, enabled: true },
    ]);
    await userEvent.click(box("Palworld"));
    await userEvent.click(save);
    expect(api.adminSetGames).toHaveBeenCalledWith(["MINECRAFT", "PALWORLD"]);
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });
});
