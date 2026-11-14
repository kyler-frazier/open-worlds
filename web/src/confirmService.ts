/**
 * In-app confirmation dialog, callable from anywhere (including plain async
 * helpers) via a module-level function backed by a mounted React host.
 *
 * Not window.confirm/prompt/alert: in some Chromium builds dismissing a native
 * dialog leaves keyboard focus wedged until the OS window is re-focused.
 */
import type { ReactNode } from "react";

export interface ConfirmOptions {
  title?: string;
  message: string;
  // Rendered under the message, for showing exactly what an action touches.
  details?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  // When set, the user must type this exact value before confirming.
  requireText?: string;
  // When set, the dialog offers an opt-out "back up first" checkbox and, if
  // checked, runs a backup to completion before resolving true.
  backup?: { worldId: string; driveLinked: boolean };
}

type Impl = (options: ConfirmOptions) => Promise<boolean>;

let impl: Impl | null = null;

export function registerConfirm(fn: Impl): void {
  impl = fn;
}

export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  if (impl) return impl(options);
  // Fallback only if the host somehow isn't mounted yet.
  return Promise.resolve(window.confirm(options.message));
}

// --- Confirm-open signal (lets busy spinners hide while a dialog is up) -------
let confirmOpen = false;
const listeners = new Set<() => void>();

export function setConfirmOpen(open: boolean): void {
  if (confirmOpen === open) return;
  confirmOpen = open;
  listeners.forEach((l) => l());
}

export function getConfirmOpen(): boolean {
  return confirmOpen;
}

export function subscribeConfirmOpen(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
