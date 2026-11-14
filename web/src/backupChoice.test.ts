import { describe, expect, it } from "vitest";
import {
  backupLabel,
  currentChoice,
  decodeChoice,
  driveLabel,
} from "./backupChoice";
import type { DriveBackup, RestorableBackup } from "./types";

describe("currentChoice / decodeChoice", () => {
  it("round-trips a tracked backup", () => {
    expect(decodeChoice(currentChoice("b1", ""))).toEqual({
      kind: "backup",
      id: "b1",
    });
  });

  it("round-trips a Drive file", () => {
    expect(decodeChoice(currentChoice("", "f1"))).toEqual({
      kind: "drive",
      id: "f1",
    });
  });

  it("prefers the tracked backup when both are set", () => {
    expect(currentChoice("b1", "f1")).toBe("backup:b1");
  });

  it("is empty for no choice", () => {
    expect(currentChoice("", "")).toBe("");
    expect(decodeChoice("")).toBeNull();
  });

  // Drive ids are opaque; only the first colon separates the kind.
  it("keeps colons inside the id", () => {
    expect(decodeChoice("drive:a:b")).toEqual({ kind: "drive", id: "a:b" });
  });

  it.each(["backup", "backup:", "other:x", ":x"])("rejects %j", (value) => {
    expect(decodeChoice(value)).toBeNull();
  });
});

describe("labels", () => {
  it("names a tracked backup by world and file", () => {
    const b = {
      world_display_name: "Alpha",
      filename: "alpha-1.tar.gz",
    } as RestorableBackup;
    expect(backupLabel(b)).toBe("Alpha · alpha-1.tar.gz");
  });

  const file = (over: Partial<DriveBackup>): DriveBackup => ({
    drive_file_id: "f1",
    filename: "x.tar.gz",
    world_subdomain: "gone",
    game: "MINECRAFT",
    size_bytes: null,
    modified_at: null,
    known: false,
    ...over,
  });

  it("leaves out a size and date it does not have", () => {
    expect(driveLabel(file({}))).toBe("gone · x.tar.gz");
  });

  it.each([
    [1, "1 MB"],
    [5 * 1024 ** 2, "5 MB"],
    [1.5 * 1024 ** 3, "1.5 GB"],
  ])("shows %d bytes as %s", (bytes, label) => {
    expect(driveLabel(file({ size_bytes: bytes }))).toBe(
      `gone · x.tar.gz · ${label}`,
    );
  });
});
