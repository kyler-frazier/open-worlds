import { ApiError, api } from "./api";
import { confirmDialog } from "./confirmService";
import type { IconChange, StagedUpload } from "./types";

/** One line per icon change, saying why -- the owner may know these files. */
function describeIconChange(c: IconChange): string {
  return c.action === "move"
    ? `  \u2022 ${c.path} will be moved to ${c.to}, where the game looks for the server icon.`
    : `  \u2022 ${c.path} will be deleted. The upload also has a server icon, and the game only shows that one.`;
}

/**
 * If a staged upload already exists (an earlier attempt that never finished),
 * offer to discard it and retry. Returns the staged upload, or null if the user
 * declined. Throws ApiError for other failures.
 */
async function preflightWithRetry(
  worldId: string,
  run: () => Promise<StagedUpload>,
): Promise<StagedUpload | null> {
  try {
    return await run();
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const discard = await confirmDialog({
        title: "Upload already staged",
        message:
          "An upload is already staged for this world — a previous attempt may not have finished. Discard it and use this one instead?",
        danger: true,
        confirmLabel: "Discard & use this",
      });
      if (!discard) return null;
      await api.deleteStagedImport(worldId);
      return await run();
    }
    throw err;
  }
}

/**
 * Confirm what the import will change beyond copying the world in -- pruned
 * "garbage", and icon files moved or deleted so the world keeps one icon --
 * then commit the staged import. One dialog, so everything that will happen
 * to the upload is said in one place.
 */
async function finishStaged(
  worldId: string,
  staged: StagedUpload,
  gameLabel: string,
): Promise<boolean> {
  const prune = staged.unknown_entries.length > 0;
  const icons = staged.icon_changes ?? [];
  if (prune || icons.length > 0) {
    const sections: string[] = [];
    if (prune) {
      const list = staged.unknown_entries
        .map((e) => `  \u2022 ${e}`)
        .join("\n");
      sections.push(
        `This content includes items that aren't part of a normal ${gameLabel} world and will be removed before importing:\n\n${list}`,
      );
    }
    if (icons.length > 0) {
      sections.push(
        `The world keeps one server icon, in server-icon.png:\n\n${icons.map(describeIconChange).join("\n")}`,
      );
    }
    const ok = await confirmDialog({
      title: prune ? "Remove extra files?" : "Before importing",
      message: `${sections.join("\n\n")}\n\nContinue?`,
      confirmLabel: prune ? "Remove & import" : "Import",
    });
    if (!ok) {
      await api.deleteStagedImport(worldId);
      return false;
    }
  }
  await api.commitImport(worldId, { prune });
  return true;
}

/** Stage an uploaded archive, confirm-prune, and commit. Returns false if cancelled. */
export async function stageAndCommitUpload(
  worldId: string,
  file: File,
  gameLabel: string,
): Promise<boolean> {
  const staged = await preflightWithRetry(worldId, () =>
    api.preflightImport(worldId, file),
  );
  if (!staged) return false;
  return finishStaged(worldId, staged, gameLabel);
}

/** Stage a Drive backup, confirm-prune, and commit. Returns false if cancelled. */
export async function stageAndCommitDrive(
  worldId: string,
  backupId: string,
  gameLabel: string,
): Promise<boolean> {
  const staged = await preflightWithRetry(worldId, () =>
    api.preflightImportFromDrive(worldId, backupId),
  );
  if (!staged) return false;
  return finishStaged(worldId, staged, gameLabel);
}

/** Same restore, for an archive we have no backup row for (its world is gone). */
export async function stageAndCommitDriveFile(
  worldId: string,
  driveFileId: string,
  gameLabel: string,
): Promise<boolean> {
  const staged = await preflightWithRetry(worldId, () =>
    api.preflightImportFromDriveFile(worldId, driveFileId),
  );
  if (!staged) return false;
  return finishStaged(worldId, staged, gameLabel);
}
