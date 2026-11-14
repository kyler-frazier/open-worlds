import type { World } from "./types";

/**
 * Shared plumbing for surfacing a world's server error: the card's error
 * section and the pop-in notifications both key off the same error, and the
 * notification's "Show details" opens the card's section.
 */

const OPEN_EVENT = "world-error:open";
const SEEN_KEY = "gp.seenWorldErrors";

/** One error occurrence. A new boot that fails again is a new key. */
export const errorKey = (w: Pick<World, "id" | "last_error_at">) =>
  `${w.id}@${w.last_error_at}`;

export const errorSectionId = (worldId: string) => `world-error-${worldId}`;

/** Open a card's error section and bring it into view. */
export function openWorldError(worldId: string): void {
  window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: worldId }));
}

export function onOpenWorldError(
  handler: (worldId: string) => void,
): () => void {
  const listener = (e: Event) => handler((e as CustomEvent<string>).detail);
  window.addEventListener(OPEN_EVENT, listener);
  return () => window.removeEventListener(OPEN_EVENT, listener);
}

/** Errors already announced, kept per browser so a reload does not repeat
 * them and an error that happened while the page was closed still is. */
export function loadSeen(): Set<string> {
  try {
    const raw = localStorage.getItem(SEEN_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

/** Persist ``seen``, trimmed to errors that still exist so it cannot grow. */
export function saveSeen(seen: Set<string>, current: Iterable<string>): void {
  const live = new Set(current);
  const kept = [...seen].filter((k) => live.has(k));
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify(kept));
  } catch {
    // Storage full or disabled: notifications just repeat on reload.
  }
}
