/**
 * Per-game additions to the web app, keyed by the API's game id.
 *
 * The API is the source of truth for which games exist and what they support
 * (`/api/games`, via `useGames`); this registry only adds presentation. A game
 * without an entry here falls back to `generic` and still works.
 */
import generic from "./generic";
import minecraft from "./minecraft";
import palworld from "./palworld";
import type { GameModule } from "./types";

const MODULES: Record<string, GameModule> = {
  MINECRAFT: minecraft,
  PALWORLD: palworld,
};

export function gameModule(game: string): GameModule {
  return MODULES[game] ?? generic;
}

/** Admin tabs games bring with them, for every game that has a module. */
export function gameAdminSections(): NonNullable<GameModule["adminSections"]> {
  return Object.values(MODULES).flatMap((m) => m.adminSections ?? []);
}

export { GamesProvider } from "./context";
export { useGames } from "./gamesContext";
export { default as SettingsFields } from "./SettingsFields";
export { changedKeys, defaultSettings, settingsFields } from "./schema";
