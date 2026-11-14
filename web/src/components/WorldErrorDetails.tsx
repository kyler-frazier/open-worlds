import { useEffect, useRef, useState } from "react";
import type { World } from "../types";
import { errorSectionId, onOpenWorldError } from "../worldErrors";

/**
 * Why the world's server failed, on its card: the line that matters, with the
 * full log excerpt one click away. Opened from a notification's "Show
 * details", which also scrolls it into view.
 */
export default function WorldErrorDetails({ world }: { world: World }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDetailsElement>(null);

  useEffect(
    () =>
      onOpenWorldError((id) => {
        if (id !== world.id) return;
        setOpen(true);
        ref.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      }),
    [world.id],
  );

  if (!world.last_error) return null;
  const when = world.last_error_at
    ? new Date(world.last_error_at).toLocaleString()
    : null;

  return (
    <details
      ref={ref}
      id={errorSectionId(world.id)}
      className="world-error"
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary>
        <span className="world-error-title">Server error</span>
        <span className="world-error-summary">
          {world.last_error_summary ?? ""}
        </span>
      </summary>
      <pre className="world-error-log">{world.last_error}</pre>
      {when && (
        <div className="muted small world-error-when">
          From the server log, {when}. Cleared the next time the world starts.
        </div>
      )}
    </details>
  );
}
