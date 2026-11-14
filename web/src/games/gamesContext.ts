import { createContext, useContext } from "react";
import type { GameInfo } from "../types";

interface Games {
  /** The games this host runs, from /api/games; empty until loaded. */
  list: GameInfo[];
  get: (game: string) => GameInfo | undefined;
  /** Display name, falling back to the id before the list arrives. */
  name: (game: string) => string;
}

export const GamesContext = createContext<Games | undefined>(undefined);

export function useGames(): Games {
  const ctx = useContext(GamesContext);
  if (!ctx) throw new Error("useGames must be used inside <GamesProvider>");
  return ctx;
}
