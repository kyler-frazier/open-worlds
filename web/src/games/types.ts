import type { ComponentType, ReactNode } from "react";
import type { GameInfo, GameSettings, World } from "../types";

/**
 * What the web app knows about one game beyond what `/api/games` says.
 *
 * Everything here is optional. A game the API reports and this app has no
 * module for still works: the settings form is built from its JSON schema and
 * the copy falls back to neutral wording (see `generic.ts`). A module only adds
 * what reads better with game-specific knowledge -- artwork, how waking a world
 * feels to a player, validation that saves a round trip.
 */
export interface GameModule {
  /** Where to anchor the card artwork (CSS background-position). The art
   *  itself is the operator's, served by the API when supplied. */
  artPosition?: string;
  /** Short values shown as tags on a world's card, after the game's name. */
  tags?: (world: World) => { text: string; kind: "type" | "version" }[];
  /** Problems with settings the API would refuse, shown while typing. */
  settingsProblems?: (settings: GameSettings) => string[];
  /** Consequences of a settings change worth confirming, in words. */
  settingsRisks?: (from: GameSettings, to: GameSettings) => string[];
  /** What happens when a player connects to a sleeping world. */
  wakeNote?: string;
  /** How long "Stopping" lasts, if that is worth saying. */
  stoppingNote?: string;
  /** Whether the first connection to a sleeping world fails, so starting it
   *  ahead of time is worth suggesting. */
  firstConnectFails?: boolean;
  /** Help under the world-archive upload field. */
  uploadHelp?: ReactNode;
  /** Where the icon shows, for the icon picker's caption. */
  iconHelp?: (fromSave: boolean) => string;
  /** The world's content add-ons tab, for games with a curated catalog. */
  WorldContent?: ComponentType<{ world: World; onClose: () => void }>;
  /** Extra admin tabs the game brings (its content catalog). */
  adminSections?: { id: string; label: string; Component: ComponentType }[];
}

export interface SettingsFieldsProps {
  game: GameInfo;
  settings: GameSettings;
  onChange: (next: GameSettings) => void;
  /** Fields in game.spec_fields are disabled while this is set. */
  specLocked?: boolean;
}
