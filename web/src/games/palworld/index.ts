import type { GameModule } from "../types";

// Mirrors the API's check: the settings file has no escape syntax and is one
// line of Key=Value pairs, so these would corrupt every setting after this one.
const UNSAFE = /["(),\r\n]/;

const palworld: GameModule = {
  // Steam updates the server in place, so the version is what it reports.
  tags: (w) =>
    w.reported_version ? [{ text: w.reported_version, kind: "version" }] : [],
  settingsProblems: (s) =>
    UNSAFE.test(String(s.server_description ?? ""))
      ? [
          "Quotes, commas, parentheses and line breaks aren't allowed in the description.",
        ]
      : [],
  // The protocol gives a relay no way to hold a joining player while the
  // server boots, so the first attempt is the one that wakes it.
  wakeNote:
    "That first attempt will fail, though: the server needs about a minute to start, so try again once and you'll be let in.",
  firstConnectFails: true,
  stoppingNote: "Palworld can take a few minutes here.",
};

export default palworld;
