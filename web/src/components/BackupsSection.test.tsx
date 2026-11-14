import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { registerConfirm } from "../confirmService";
import { makeWorld } from "../test/fixtures";
import type { Backup } from "../types";
import BackupsSection from "./BackupsSection";

const backup = (over: Partial<Backup> = {}): Backup => ({
  id: "b1",
  kind: "MANUAL",
  status: "SUCCESS",
  filename: "alpha-1.tar.gz",
  size_bytes: 3 * 1024 ** 2,
  created_at: "2026-01-01T00:00:00Z",
  completed_at: "2026-01-01T00:01:00Z",
  error: null,
  ...over,
});

let confirmAnswer: boolean;
beforeEach(() => {
  confirmAnswer = false;
  registerConfirm(async () => confirmAnswer);
});

async function renderWith(
  backups: Backup[],
  { canModify = true, driveLinked = true } = {},
) {
  vi.spyOn(api, "listBackups").mockResolvedValue(backups);
  render(
    <BackupsSection
      world={makeWorld()}
      driveLinked={driveLinked}
      canModify={canModify}
      onChanged={() => {}}
    />,
  );
  await screen.findByText(
    backups.length ? backups[0].filename : "No backups yet.",
  );
}

describe("BackupsSection", () => {
  it("lists backups with their size", async () => {
    await renderWith([backup()]);
    expect(screen.getByText("3.0 MB")).toBeInTheDocument();
  });

  it("shows why a backup failed", async () => {
    await renderWith([
      backup({ status: "FAILED", error: "Drive quota exceeded" }),
    ]);
    expect(screen.getByTitle("Drive quota exceeded")).toHaveTextContent(
      "FAILED",
    );
  });

  it("deletes only once confirmed", async () => {
    vi.spyOn(api, "deleteBackup").mockResolvedValue(undefined as never);
    await renderWith([backup()]);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(api.deleteBackup).not.toHaveBeenCalled();

    confirmAnswer = true;
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(api.deleteBackup).toHaveBeenCalledWith("w1", "b1");
  });

  it("cannot delete a backup still being written", async () => {
    await renderWith([backup({ status: "RUNNING" })]);
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  });

  it("gives collaborators the list but no actions", async () => {
    await renderWith([backup()], { canModify: false });
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("needs Drive linked to back up", async () => {
    await renderWith([], { driveLinked: false });
    expect(screen.getByRole("button", { name: "Back up now" })).toBeDisabled();
  });
});
