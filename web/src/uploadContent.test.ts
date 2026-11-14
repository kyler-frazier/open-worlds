import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api";
import { registerConfirm, type ConfirmOptions } from "./confirmService";
import type { StagedUpload } from "./types";
import { stageAndCommitUpload } from "./uploadContent";

const staged = (over: Partial<StagedUpload> = {}): StagedUpload => ({
  id: "s1",
  filename: "world.zip",
  size_bytes: 1,
  file_count: 1,
  unknown_entries: [],
  icon_changes: [],
  ...over,
});

const file = new File(["x"], "world.zip");
let answers: boolean[];
let asked: ConfirmOptions[];

beforeEach(() => {
  answers = [];
  asked = [];
  registerConfirm(async (o) => {
    asked.push(o);
    return answers.shift() ?? false;
  });
  vi.spyOn(api, "commitImport").mockResolvedValue(undefined as never);
  vi.spyOn(api, "deleteStagedImport").mockResolvedValue(undefined as never);
});

describe("stageAndCommitUpload", () => {
  it("commits a clean upload without asking", async () => {
    vi.spyOn(api, "preflightImport").mockResolvedValue(staged());
    expect(await stageAndCommitUpload("w1", file, "Minecraft")).toBe(true);
    expect(asked).toEqual([]);
    expect(api.commitImport).toHaveBeenCalledWith("w1", { prune: false });
  });

  it("names extra files and prunes them once confirmed", async () => {
    vi.spyOn(api, "preflightImport").mockResolvedValue(
      staged({ unknown_entries: ["junk.txt"] }),
    );
    answers = [true];
    expect(await stageAndCommitUpload("w1", file, "Minecraft")).toBe(true);
    expect(asked[0].message).toContain("junk.txt");
    expect(api.commitImport).toHaveBeenCalledWith("w1", { prune: true });
  });

  it("discards the staged upload when the prune is declined", async () => {
    vi.spyOn(api, "preflightImport").mockResolvedValue(
      staged({ unknown_entries: ["junk.txt"] }),
    );
    expect(await stageAndCommitUpload("w1", file, "Minecraft")).toBe(false);
    expect(api.deleteStagedImport).toHaveBeenCalledWith("w1");
    expect(api.commitImport).not.toHaveBeenCalled();
  });

  it("explains icon moves before importing", async () => {
    vi.spyOn(api, "preflightImport").mockResolvedValue(
      staged({
        icon_changes: [
          { action: "move", path: "world/icon.png", to: "server-icon.png" },
        ],
      }),
    );
    answers = [true];
    await stageAndCommitUpload("w1", file, "Minecraft");
    expect(asked[0].message).toContain(
      "world/icon.png will be moved to server-icon.png",
    );
    expect(api.commitImport).toHaveBeenCalledWith("w1", { prune: false });
  });

  it("offers to replace an upload left staged by an earlier attempt", async () => {
    const preflight = vi
      .spyOn(api, "preflightImport")
      .mockRejectedValueOnce(new ApiError(409, "already staged"))
      .mockResolvedValueOnce(staged());
    answers = [true];
    expect(await stageAndCommitUpload("w1", file, "Minecraft")).toBe(true);
    expect(api.deleteStagedImport).toHaveBeenCalledWith("w1");
    expect(preflight).toHaveBeenCalledTimes(2);
  });

  it("leaves the earlier upload alone when replacing it is declined", async () => {
    vi.spyOn(api, "preflightImport").mockRejectedValue(
      new ApiError(409, "already staged"),
    );
    expect(await stageAndCommitUpload("w1", file, "Minecraft")).toBe(false);
    expect(api.deleteStagedImport).not.toHaveBeenCalled();
  });

  it("passes other failures through", async () => {
    vi.spyOn(api, "preflightImport").mockRejectedValue(
      new ApiError(413, "too big"),
    );
    await expect(stageAndCommitUpload("w1", file, "Minecraft")).rejects.toThrow(
      "too big",
    );
    expect(asked).toEqual([]);
  });
});
