import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { makeGame, makeLimits, makeWorld } from "../test/fixtures";
import type { DriveBackup, GameInfo, RestorableBackup } from "../types";
import CreateWorldModal from "./CreateWorldModal";

const minecraft = makeGame({
  settings_schema: {
    properties: {
      version: { title: "Version", type: "string", default: "1.21.4" },
    },
  },
});
const palworld = makeGame({
  game: "PALWORLD",
  display_name: "Palworld",
  supports_server_password: true,
  allocates_port: true,
  default_memory_mb: 16384,
  ports_available: 1,
  settings_schema: {
    properties: {
      server_description: { title: "Description", type: "string" },
    },
  },
});

async function renderWith(games: GameInfo[]) {
  vi.spyOn(api, "getLimits").mockResolvedValue(makeLimits({ games }));
  const onCreated = vi.fn();
  const onClose = vi.fn();
  render(
    <CreateWorldModal driveLinked onCreated={onCreated} onClose={onClose} />,
  );
  await screen.findByText(/Limits: up to/);
  return { onCreated, onClose };
}

async function fillName(sub: string) {
  await userEvent.type(screen.getByLabelText("Display name"), "My World");
  await userEvent.type(screen.getByLabelText(/^Subdomain/), sub);
}

describe("CreateWorldModal", () => {
  it("hides the game picker when only one game is offered", async () => {
    await renderWith([minecraft]);
    expect(screen.queryByLabelText("Game")).toBeNull();
    expect(screen.getByLabelText("Version")).toHaveValue("1.21.4");
  });

  it("creates an empty world with the game's defaults", async () => {
    vi.spyOn(api, "createWorld").mockResolvedValue(makeWorld());
    const { onCreated, onClose } = await renderWith([minecraft]);
    await fillName("my-world");
    await userEvent.click(screen.getByRole("button", { name: "Create world" }));
    expect(api.createWorld).toHaveBeenCalledWith({
      display_name: "My World",
      subdomain: "my-world",
      game: "MINECRAFT",
      max_players: 10,
      settings: { version: "1.21.4" },
      memory_mb: 4096,
    });
    expect(onCreated).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the subdomain to DNS-label characters", async () => {
    await renderWith([minecraft]);
    await userEvent.type(screen.getByLabelText(/^Subdomain/), "My_World!.1");
    expect(screen.getByLabelText(/^Subdomain/)).toHaveValue("myworld1");
  });

  // Another game's keys would be refused, and its memory figure means
  // something else.
  it("re-seeds settings and memory when the game changes", async () => {
    await renderWith([minecraft, palworld]);
    await userEvent.selectOptions(screen.getByLabelText("Game"), "PALWORLD");
    expect(screen.queryByLabelText("Version")).toBeNull();
    expect(screen.getByLabelText("Description")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Memory \(MB\)/)).toHaveValue(16384);
    expect(screen.getByText(/1 Palworld port left/)).toBeInTheDocument();
  });

  it("keeps memory the user chose across a game change", async () => {
    await renderWith([minecraft, palworld]);
    const memory = screen.getByLabelText(/^Memory \(MB\)/);
    await userEvent.clear(memory);
    await userEvent.type(memory, "8192");
    await userEvent.selectOptions(screen.getByLabelText("Game"), "PALWORLD");
    expect(memory).toHaveValue(8192);
  });

  it("blocks creating while a setting would be refused", async () => {
    await renderWith([palworld]);
    await userEvent.type(screen.getByLabelText("Description"), "a, b");
    expect(
      screen.getByText(/aren't allowed in the description/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create world" })).toBeDisabled();
  });

  it("asks for a file before uploading", async () => {
    vi.spyOn(api, "createWorld");
    await renderWith([minecraft]);
    await fillName("w");
    await userEvent.click(screen.getByRole("button", { name: "Upload file" }));
    await userEvent.click(screen.getByRole("button", { name: "Create world" }));
    expect(
      screen.getByText(/Choose a .zip or .tar.gz file/),
    ).toBeInTheDocument();
    expect(api.createWorld).not.toHaveBeenCalled();
  });

  // One game's save cannot seed another's world.
  it("offers only backups of the chosen game", async () => {
    const restorable = (game: string, name: string) =>
      ({
        id: name,
        game,
        world_display_name: name,
        filename: `${name}.tar.gz`,
      }) as RestorableBackup;
    vi.spyOn(api, "listRestorableBackups").mockResolvedValue([
      restorable("MINECRAFT", "Mc"),
      restorable("PALWORLD", "Pal"),
    ]);
    vi.spyOn(api, "listDriveBackups").mockResolvedValue([
      {
        drive_file_id: "f1",
        filename: "old.tar.gz",
        world_subdomain: "gone",
        game: "MINECRAFT",
        size_bytes: null,
        modified_at: null,
        known: false,
      } satisfies DriveBackup,
    ]);
    await renderWith([minecraft, palworld]);
    await userEvent.click(
      screen.getByRole("button", { name: "From Google Drive" }),
    );
    const picker = await screen.findByLabelText("Backup to restore");
    const options = [...picker.querySelectorAll("option")].map(
      (o) => o.textContent,
    );
    expect(options).toEqual([
      "Select a backup…",
      "Mc · Mc.tar.gz",
      "gone · old.tar.gz",
    ]);

    await userEvent.selectOptions(picker, "backup:Mc");
    await userEvent.selectOptions(screen.getByLabelText("Game"), "PALWORLD");
    expect(screen.getByLabelText("Backup to restore")).toHaveValue("");
  });
});
