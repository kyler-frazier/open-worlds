import { useEffect, useLayoutEffect, useRef } from "react";

/**
 * Run `load` on an interval, but only while the tab is visible, and once
 * immediately on becoming visible so a returning user never sees stale data.
 */
export function usePolling(
  load: () => void | Promise<void>,
  intervalMs: number,
  options: { skip?: () => boolean } = {},
) {
  // Held in a ref so a changing callback identity does not tear down and
  // rebuild the timer on every render. Updated in a layout effect rather than
  // during render; layout effects run before the effect below, so the first
  // load still sees the current callback.
  const loadRef = useRef(load);
  const skipRef = useRef(options.skip);
  useLayoutEffect(() => {
    loadRef.current = load;
    skipRef.current = options.skip;
  });

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | undefined;

    const tick = () => {
      if (skipRef.current?.()) return;
      void loadRef.current();
    };

    const start = () => {
      if (timer !== undefined) return;
      timer = setInterval(tick, intervalMs);
    };

    const stop = () => {
      if (timer === undefined) return;
      clearInterval(timer);
      timer = undefined;
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        // Immediately, not on the next tick: the whole point of returning to
        // the tab is to see the current state.
        tick();
        start();
      } else {
        stop();
      }
    };

    // First load happens regardless -- a page that mounts hidden (restored
    // session, opened in a background tab) should still have data ready.
    void loadRef.current();
    if (document.visibilityState === "visible") start();

    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [intervalMs]);
}
