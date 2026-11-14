import { useState, type CSSProperties } from "react";
import { ApiError, api } from "../api";
import type { World } from "../types";
import CopyableText from "./CopyableText";
import EditWorldModal from "./EditWorldModal";
import InfoTooltip from "./InfoTooltip";
import StatusBadge from "./StatusBadge";
import WorldErrorDetails from "./WorldErrorDetails";
import { worldStatus } from "../worldStatus";
import { useAuth } from "../auth";
import { isStrictOwner } from "../access";
import { gameModule, useGames } from "../games";

interface Props {
  world: World;
  driveLinked: boolean;
  showOwner?: boolean;
  onChanged: () => void;
  /** Takes the world a start or stop answered with, so the card shows it now
   * rather than on the next poll. */
  onUpdated: (world: World) => void;
}

export default function WorldCard({
  world,
  driveLinked,
  showOwner,
  onChanged,
  onUpdated,
}: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showEdit, setShowEdit] = useState(false);
  const { me } = useAuth();
  // Someone else's world, shared with you. Worth saying plainly: it is why
  // this card has fewer buttons than the one next to it.
  const shared = !isStrictOwner(world, me);
  const games = useGames();
  const mod = gameModule(world.game);
  const artUrl = games.get(world.game)?.art_url;

  // Start and stop answer with the world's new state. Showing it straight away
  // is what makes a stop visible: a sleeping world goes to Off within the
  // request, and waiting for the next poll to say so meant the card read
  // Sleeping, then jumped to Off, with Stopping only ever glimpsed. The buttons
  // stay locked from the click until this lands, so nothing can be started
  // while the old container is still being removed.
  const power = async (label: string, fn: () => Promise<World>) => {
    setBusy(label);
    setError(null);
    try {
      onUpdated(await fn());
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
    } finally {
      setBusy(null);
    }
  };

  // A stop runs in the background after the API answers, so the card stays
  // locked until the poll shows the world Off/Sleeping. Nothing here can be
  // meaningfully done to a world that is mid-shutdown.
  // A Turn off in flight reads as Stopping at once, not only once the API
  // answers. A sleeping world's stop is over within the request, so the API
  // answers Off and the card would otherwise never show Stopping at all -- this
  // guarantees at least a frame of it, whichever world is being turned off.
  const state: World["state"] = busy === "stop" ? "STOPPING" : world.state;
  const stopping = state === "STOPPING";
  const isBusy = busy !== null || stopping;

  // Tints the card by the same rule the badge labels it, so a glance at the
  // grid answers "which of these can I join right now" without reading.
  const status = worldStatus(state, world.readiness);

  return (
    <div
      className={`card world-card world-card--${status}${artUrl ? " world-card--art" : ""}`}
    >
      {/* The game's key art, faded to a watermark. Its own element rather than
          the card's background, which the status tint already owns. */}
      {artUrl && (
        <div
          className="world-card-art"
          aria-hidden="true"
          style={
            {
              "--world-art": `url("${artUrl}")`,
              "--world-art-position": mod.artPosition ?? "center",
            } as CSSProperties
          }
        />
      )}
      <div className="world-head">
        {world.icon_version !== null && (
          <img
            className="world-icon"
            src={api.worldIconUrl(world.id, world.icon_version)}
            alt=""
            width={40}
            height={40}
          />
        )}
        <div className="world-head-main">
          <h3>{world.display_name}</h3>
          <CopyableText
            value={world.connect_address}
            label="Copy connect address"
          />
          {(showOwner || shared) && world.owner_email && (
            <div className="muted small">owner: {world.owner_email}</div>
          )}
        </div>
        <div className="world-corner">
          <div className="world-status">
            <StatusBadge state={state} readiness={world.readiness} />
            <InfoTooltip label="How world states work">
              <strong>Running</strong> — online and accepting players.
              <br />
              <strong>Sleeping</strong> — automatically paused after ~10 minutes
              with no players, to save resources. Connecting wakes it.{" "}
              {mod.wakeNote}
              <br />
              <strong>Stopping</strong> — shutting down and saving.{" "}
              {mod.stoppingNote} It will read Off (or Sleeping) when done.
              <br />
              <strong>Off</strong> — stopped by you. It won't wake on connect;
              use "Turn on" to start it again.
              <br />
              <strong>Error</strong> — the server stopped unexpectedly. It won't
              wake on connect either; fix the cause, then use "Turn on".
              <br />
              Your world data is always kept safe in every state.
              {mod.firstConnectFails && (
                <>
                  <br />
                  <br />
                  <strong>Playing soon?</strong> Press "Turn on" a minute before
                  your friends join. The world will be up and waiting, and
                  nobody has to hit the failed first connection.
                </>
              )}
            </InfoTooltip>
          </div>
          {/* Occupancy sits with the status rather than among the spec badges:
              it is the only figure here that changes minute to minute, and it
              answers the same question the status does -- is anyone on it. */}
          <div
            className="world-players muted small"
            title="Players online / capacity"
          >
            {state === "RUNNING" ? (
              <>
                <strong>{world.player_count}</strong>
                <span aria-hidden="true">/</span>
                {world.max_players}
              </>
            ) : (
              <>max {world.max_players}</>
            )}
            <span className="world-players-label"> players</span>
          </div>
        </div>
      </div>

      <div className="world-meta">
        {/* The game leads: it is what tells the user why this card's other
            badges differ from its neighbour's. Each kind carries its own hue
            so the row can be scanned by position and colour rather than read. */}
        {shared && (
          <span
            className="tag tag-shared"
            title={`Shared with you by ${world.owner_email ?? "its owner"} — ${world.access_level.toLowerCase()} access`}
          >
            shared · {world.access_level.toLowerCase()}
          </span>
        )}
        <span className="tag tag-game">{games.name(world.game)}</span>
        {(mod.tags?.(world) ?? []).map((t) => (
          <span key={t.text} className={`tag tag-${t.kind}`}>
            {t.text}
          </span>
        ))}
        <span className="tag tag-memory">{world.memory_mb} MB</span>
        {world.server_password && (
          <span
            className="tag tag-lock"
            title="Players need this password to join"
          >
            🔒 password
          </span>
        )}
        {/* A relaxed fence is easy to forget once the debugging is done, so
            it stays visible on the card, not only in settings. */}
        {world.egress_open_exception && (
          <span
            className="tag tag-warn"
            title="Unknown network connections are allowed for this world (logged, not blocked)."
          >
            ⚠️ Network Open
          </span>
        )}
        {world.auto_backup_enabled && (
          <span
            className="tag tag-accent"
            // The schedule and retention still matter, just not at a glance --
            // the title keeps them a hover away and the Backups tab owns them.
            title={`Backs up ${world.auto_backup_interval.toLowerCase()}, keeping ${world.auto_backup_max}`}
          >
            auto-backup
          </span>
        )}
      </div>

      <WorldErrorDetails world={world} />

      {error && <div className="error-banner">{error}</div>}

      <div className="world-actions">
        {/* A sleeping world gets a wake action of its own, not just "Turn off".
            Starting it deliberately ahead of time is the answer to the failed
            first connection: for some games the protocol gives us no way to tell a
            joining player "hold on, booting", so the only good outcome is the
            world already being up when they try. Without this the owner's only
            route to a warm world was Turn off then Turn on. */}
        {state === "SLEEPING" && (
          <button
            className="btn btn-primary"
            disabled={isBusy}
            title="Start the world now so it's ready before anyone joins"
            onClick={() => power("start", () => api.startWorld(world.id))}
          >
            {busy === "start" ? "Waking…" : "Wake now"}
          </button>
        )}
        {state === "RUNNING" || state === "SLEEPING" || stopping ? (
          <button
            className="btn btn-secondary"
            disabled={isBusy}
            title={
              stopping
                ? "Shutting down gracefully; this can take a few minutes"
                : undefined
            }
            onClick={() => power("stop", () => api.stopWorld(world.id))}
          >
            {stopping ? "Stopping…" : "Turn off"}
          </button>
        ) : (
          <button
            className="btn btn-primary"
            disabled={isBusy || world.state === "DELETING"}
            onClick={() => power("start", () => api.startWorld(world.id))}
          >
            {busy === "start" ? "Starting…" : "Turn on"}
          </button>
        )}

        {/* Pushed to the trailing corner: Backups and Delete now live behind
            it, so it is a way in rather than one action among several, and it
            should not compete with the power controls for attention. */}
        <button
          className="btn btn-ghost world-edit"
          disabled={isBusy}
          title="Settings, backups and deletion"
          // The glyph carries no text for a screen reader, so name the button.
          aria-label="Settings, backups and deletion"
          onClick={() => setShowEdit(true)}
        >
          {/* An inline SVG, not a ⚙ glyph, which renders small and as a colour
              emoji on iOS. Eight teeth on a 45-degree pitch centred on (12,12),
              so the hover rotation lands one tooth on; the hole uses evenodd.
              Teeth are trapezoids whose tip width equals the root gap (2.51),
              tapering from a 3.89 base. */}
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path
              fill="currentColor"
              fillRule="evenodd"
              d="M22.93 10.75L22.93 13.25L19.97 13.95L19.01 16.26L20.61 18.84L18.84 20.61L16.26 19.01L13.95 19.97L13.25 22.93L10.75 22.93L10.05 19.97L7.74 19.01L5.16 20.61L3.39 18.84L4.99 16.26L4.03 13.95L1.07 13.25L1.07 10.75L4.03 10.05L4.99 7.74L3.39 5.16L5.16 3.39L7.74 4.99L10.05 4.03L10.75 1.07L13.25 1.07L13.95 4.03L16.26 4.99L18.84 3.39L20.61 5.16L19.01 7.74L19.97 10.05ZM7.6 12A4.4 4.4 0 1 0 16.4 12A4.4 4.4 0 1 0 7.6 12Z"
            />
          </svg>
        </button>
      </div>

      {showEdit && (
        <EditWorldModal
          world={world}
          driveLinked={driveLinked}
          onClose={() => setShowEdit(false)}
          onSaved={onChanged}
          onDeleted={onChanged}
        />
      )}
    </div>
  );
}
