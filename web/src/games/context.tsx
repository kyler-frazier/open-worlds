import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import type { GameInfo } from "../types";
import { GamesContext } from "./gamesContext";

export function GamesProvider({ children }: { children: ReactNode }) {
  const { me } = useAuth();
  const [list, setList] = useState<GameInfo[]>([]);
  useEffect(() => {
    if (!me) return;
    api
      .listGames()
      .then(setList)
      .catch(() => setList([]));
  }, [me]);
  const get = (game: string) => list.find((g) => g.game === game);
  const name = (game: string) => get(game)?.display_name ?? game;
  return (
    <GamesContext.Provider value={{ list, get, name }}>
      {children}
    </GamesContext.Provider>
  );
}
