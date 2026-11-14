import { api } from "./api";

const POLL_MS = 2500;
const TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Start a manual backup and wait until it actually finishes.
 *
 * Destructive actions (delete, replace) are synchronous or wipe data, so a
 * merely-queued backup wouldn't protect them — we must block until the backup
 * reaches SUCCESS before the caller proceeds. Throws on failure/timeout.
 */
export async function backupAndWait(worldId: string): Promise<void> {
  const created = await api.createBackup(worldId);
  const deadline = Date.now() + TIMEOUT_MS;
  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    const current = (await api.listBackups(worldId)).find(
      (b) => b.id === created.id,
    );
    if (!current) continue;
    if (current.status === "SUCCESS") return;
    if (current.status === "FAILED") {
      throw new Error(current.error || "Backup failed");
    }
  }
  throw new Error(
    "Backup is taking too long; try again or proceed without it.",
  );
}
