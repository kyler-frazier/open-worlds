import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";
import { api } from "../api";
import { AuthProvider } from "../auth";
import { GamesProvider } from "../games";
import type { GameInfo, Me } from "../types";
import { makeGame, makeMe } from "./fixtures";

/**
 * Render inside the providers the app wraps every page in, signed in as `me`.
 * Resolves once the signed-in user has loaded; `ready` is text to wait for,
 * since the providers fetch before the tree settles.
 */
export async function renderSignedIn(
  ui: ReactElement,
  {
    me = makeMe(),
    games = [makeGame()],
    ready,
  }: { me?: Me; games?: GameInfo[]; ready?: string | RegExp } = {},
) {
  vi.spyOn(api, "me").mockResolvedValue(me);
  vi.spyOn(api, "listGames").mockResolvedValue(games);
  const result = render(
    <AuthProvider>
      <GamesProvider>{ui}</GamesProvider>
    </AuthProvider>,
  );
  if (ready) await screen.findByText(ready);
  return result;
}
