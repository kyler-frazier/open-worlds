import type { Readiness, WorldState } from "../types";

const LABELS: Record<WorldState, string> = {
  RUNNING: "Running",
  SLEEPING: "Sleeping",
  STOPPING: "Stopping…",
  OFF: "Off",
  DELETING: "Deleting",
  ERROR: "Error",
};

interface Props {
  state: WorldState;
  /** Optional: refines RUNNING into starting / ready / unresponsive. */
  readiness?: Readiness;
}

/**
 * The world's status. RUNNING is not shown as "Running" until the server is
 * accepting players, or the first join fails mysteriously.
 */
export default function StatusBadge({ state, readiness }: Props) {
  if (state === "RUNNING" && readiness === "STARTING") {
    return (
      <span
        className="badge badge-starting"
        title="The server is booting — not joinable yet"
      >
        <span className="dot" />
        Starting…
      </span>
    );
  }
  if (state === "RUNNING" && readiness === "UNRESPONSIVE") {
    return (
      <span
        className="badge badge-error"
        title="The server has been up for a while but isn't answering. It may be stuck; try turning it off and on again."
      >
        <span className="dot" />
        Not responding
      </span>
    );
  }
  return (
    <span className={`badge badge-${state.toLowerCase()}`}>
      <span className="dot" />
      {LABELS[state]}
    </span>
  );
}
