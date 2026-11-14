import { confirmDialog } from "./confirmService";

/**
 * Require the user to type a world's subdomain to confirm a destructive or
 * breaking action. Optionally offers an opt-out "back up first" checkbox.
 * Resolves true only if confirmed (and the backup, if requested, succeeded).
 */
export function confirmSubdomain(
  subdomain: string,
  message: string,
  backup?: { worldId: string; driveLinked: boolean },
): Promise<boolean> {
  return confirmDialog({
    title: "Please confirm",
    message,
    requireText: subdomain,
    danger: true,
    confirmLabel: "Proceed",
    backup,
  });
}
