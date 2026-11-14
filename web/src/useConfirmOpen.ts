import { useSyncExternalStore } from "react";
import { getConfirmOpen, subscribeConfirmOpen } from "./confirmService";

/** True while an in-app confirm dialog is showing. */
export function useConfirmOpen(): boolean {
  return useSyncExternalStore(subscribeConfirmOpen, getConfirmOpen);
}
