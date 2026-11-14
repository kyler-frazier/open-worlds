import type { GameModule } from "./types";

/** For a game the API offers that this app has no module for. */
const generic: GameModule = {
  tags: (w) =>
    w.reported_version ? [{ text: w.reported_version, kind: "version" }] : [],
  wakeNote: "The first connection may take a little longer while it starts.",
};

export default generic;
