import { useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiError, api } from "../api";
import type {
  DriveBackup,
  GameInfo,
  GameSettings,
  Limits,
  RestorableBackup,
} from "../types";
import { defaultSettings, gameModule, SettingsFields } from "../games";
import { useConfirmOpen } from "../useConfirmOpen";
import {
  stageAndCommitDrive,
  stageAndCommitDriveFile,
  stageAndCommitUpload,
} from "../uploadContent";
import SecretField from "./SecretField";
import {
  backupLabel,
  currentChoice,
  decodeChoice,
  driveLabel,
} from "../backupChoice";

function formatGB(bytes: number): string {
  const gb = bytes / 1024 ** 3;
  return gb >= 1
    ? `${gb % 1 === 0 ? gb : gb.toFixed(1)} GB`
    : `${Math.round(bytes / 1024 ** 2)} MB`;
}

interface Props {
  driveLinked: boolean;
  onClose: () => void;
  onCreated: () => void;
}

type Source = "empty" | "upload" | "drive";

export default function CreateWorldModal({
  driveLinked,
  onClose,
  onCreated,
}: Props) {
  // The chosen game drives the whole form: which fields exist, what they are
  // capped at, and what the connect address will look like.
  const [game, setGame] = useState<GameInfo | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [subdomain, setSubdomain] = useState("");
  // The game's own settings, from its schema. Re-seeded from the schema's
  // defaults when the game changes: another game's keys would be refused.
  const [settings, setSettings] = useState<GameSettings>({});
  const [memory, setMemory] = useState(4096);
  const [memoryEdited, setMemoryEdited] = useState(false);
  const [maxPlayers, setMaxPlayers] = useState(16);
  const [serverPassword, setServerPassword] = useState("");
  const mod = gameModule(game?.game ?? "");
  // What the API would refuse, surfaced while typing.
  const settingsProblems = mod.settingsProblems?.(settings) ?? [];

  const [source, setSource] = useState<Source>("empty");
  const [file, setFile] = useState<File | null>(null);
  const [backups, setBackups] = useState<RestorableBackup[] | null>(null);
  // A Drive archive with no backup row. Mutually exclusive with selectedBackup:
  // picking in one list clears the other, so there is always one chosen source.
  const [selectedDriveFile, setSelectedDriveFile] = useState("");
  const [selectedBackup, setSelectedBackup] = useState<string>("");
  // Archives with no backup row, folded into the same list below.
  const [driveFiles, setDriveFiles] = useState<DriveBackup[] | null>(null);

  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [limits, setLimits] = useState<Limits | null>(null);
  const confirmOpen = useConfirmOpen();

  // Fall back to a neutral placeholder only until the limits response lands.
  const domain = limits?.servers_domain || "servers.example.com";

  useEffect(() => {
    api
      .getLimits()
      .then((l) => {
        setLimits(l);
        setMaxPlayers(l.default_max_players);
        // Default to the first game the host offers. With only one enabled the
        // picker collapses to a label, so this is the whole choice.
        setGame((cur) => cur ?? l.games[0] ?? null);
      })
      .catch(() => setLimits(null));
  }, []);

  // Prefills that follow the chosen game. Done during render when `game`
  // changes rather than in an effect, so the form never paints a frame with the
  // previous game's values (React's "adjusting state when a prop changes").
  const [prefilledFor, setPrefilledFor] = useState(game);
  if (game !== prefilledFor) {
    setPrefilledFor(game);
    // The schema's defaults are this host's: a game that pins versions offers
    // its latest stable release, a real value the user can keep or change.
    setSettings(game ? defaultSettings(game) : {});
    // Memory follows the chosen game, not the host, because the figure means
    // different things per game — reserved heap for one, a ceiling for
    // another — so switching games must re-prefill rather than carry the old
    // number across. Stops once the user has picked their own.
    if (game && !memoryEdited) setMemory(game.default_memory_mb);
  }

  useEffect(() => {
    if (source === "drive" && driveLinked && backups === null) {
      api
        .listRestorableBackups()
        .then(setBackups)
        .catch(() => setBackups([]));
    }
  }, [source, driveLinked, backups]);

  // Creating a world offers both kinds in one list, so unlike the replace tab
  // this cannot wait for someone to expand a section -- it loads with the
  // tracked backups.
  useEffect(() => {
    if (source === "drive" && driveLinked && driveFiles === null) {
      api
        .listDriveBackups()
        .then((all) => setDriveFiles(all.filter((f) => !f.known)))
        .catch(() => setDriveFiles([]));
    }
  }, [source, driveLinked, driveFiles]);

  // Narrowed to the game picked above: one game's save cannot seed another's
  // world. An archive with no recorded game never matches; it is reachable only
  // through an existing world's Replace tab.
  const gameBackups = useMemo(
    () => (backups ?? []).filter((b) => b.game === game?.game),
    [backups, game],
  );
  const gameFiles = useMemo(
    () => (driveFiles ?? []).filter((f) => f.game === game?.game),
    [driveFiles, game],
  );

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!game) {
      setError("Choose which game this world is for.");
      return;
    }
    if (source === "upload" && !file) {
      setError("Choose a .zip or .tar.gz file, or start from an empty world.");
      return;
    }
    if (source === "drive" && !selectedBackup && !selectedDriveFile) {
      setError("Select a backup to restore, or start from an empty world.");
      return;
    }
    setBusy(true);
    setError(null);
    setProgress("Creating world…");
    try {
      const world = await api.createWorld({
        display_name: displayName,
        subdomain,
        game: game.game,
        max_players: maxPlayers,
        // Only sent for games that have one; the API rejects it otherwise
        // rather than silently ignoring a setting the user thought took effect.
        ...(game.supports_server_password && serverPassword
          ? { server_password: serverPassword }
          : {}),
        settings,
        memory_mb: memory,
      });
      if (source !== "empty") {
        setProgress("Uploading and validating content…");
        try {
          const committed =
            source === "upload" && file
              ? await stageAndCommitUpload(world.id, file, game.display_name)
              : selectedDriveFile
                ? await stageAndCommitDriveFile(
                    world.id,
                    selectedDriveFile,
                    game.display_name,
                  )
                : await stageAndCommitDrive(
                    world.id,
                    selectedBackup,
                    game.display_name,
                  );
          if (!committed) {
            // User backed out of the upload — roll back the just-created empty
            // world so cancelling leaves nothing behind.
            setProgress("Cancelling…");
            try {
              await api.deleteWorld(world.id);
              onCreated();
              onClose();
            } catch {
              // Cleanup failed; keep the world but tell the user it's empty.
              onCreated();
              setError(
                `"${world.display_name}" was created but no content was imported. Load content later from its Edit menu, or delete it.`,
              );
              setBusy(false);
              setProgress(null);
            }
            return;
          }
        } catch (importErr) {
          setError(
            (importErr instanceof ApiError
              ? importErr.message
              : "Import failed") +
              ` — the world "${world.display_name}" was created but is empty; you can load content from its Edit menu or delete it.`,
          );
          onCreated();
          setBusy(false);
          setProgress(null);
          return;
        }
      }
      onCreated();
      onClose();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to create world",
      );
      setBusy(false);
      setProgress(null);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal card" onClick={(e) => e.stopPropagation()}>
        <h2>New world</h2>
        {limits && (
          <p className="hint muted small">
            Limits: up to <strong>{limits.max_worlds_per_user}</strong> worlds
            per account, and uploaded worlds up to{" "}
            <strong>{formatGB(limits.max_upload_bytes)}</strong>.
          </p>
        )}
        <form onSubmit={submit} className="form">
          {limits && limits.games.length > 1 && (
            <label>
              Game
              <select
                value={game?.game ?? ""}
                onChange={(e) => {
                  setGame(
                    limits.games.find((g) => g.game === e.target.value) ?? null,
                  );
                  // The chosen backup belonged to the game being left, and the
                  // list it came from is about to stop containing it. Leaving
                  // the id set would submit a restore the form no longer shows.
                  setSelectedBackup("");
                  setSelectedDriveFile("");
                }}
              >
                {limits.games.map((g) => (
                  <option key={g.game} value={g.game}>
                    {g.display_name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            Display name
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="My survival world"
              required
            />
          </label>
          <label>
            Subdomain
            <input
              value={subdomain}
              onChange={(e) =>
                // Keep it to valid DNS-label characters as the user types; the
                // backend enforces the full rule. (Avoids a browser `pattern`
                // attribute, whose regex engine differs across browsers.)
                setSubdomain(
                  e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""),
                )
              }
              placeholder="my-world"
              required
            />
            <span className="hint muted">
              {game?.allocates_port ? (
                <>
                  Players connect at{" "}
                  <code>
                    {subdomain || "my-world"}.{domain}:PORT
                  </code>{" "}
                  — a port is assigned when the world is created, and you'll see
                  the full address on its card.
                </>
              ) : (
                <>
                  Players connect at{" "}
                  <code>
                    {subdomain || "my-world"}.{domain}
                  </code>
                </>
              )}
            </span>
          </label>
          {/* The game's own settings, from the schema the API publishes.
              Showing a field the game does not have would promise a setting
              that silently does nothing, so only its own appear. */}
          {game && (
            <SettingsFields
              game={game}
              settings={settings}
              onChange={setSettings}
            />
          )}
          <div className="form-row">
            <label>
              Memory (MB)
              <input
                type="number"
                min={limits?.min_memory_mb ?? 1024}
                max={limits?.max_memory_mb ?? 32768}
                step={1024}
                value={memory}
                onChange={(e) => {
                  setMemoryEdited(true);
                  setMemory(Number(e.target.value));
                }}
              />
              {limits && (
                <span className="hint muted">
                  {limits.min_memory_mb}–{limits.max_memory_mb} MB on this
                  server.
                </span>
              )}
            </label>
            <label>
              Max players
              <input
                type="number"
                min={1}
                max={limits?.max_players_cap ?? 64}
                value={maxPlayers}
                onChange={(e) => setMaxPlayers(Number(e.target.value))}
              />
            </label>
          </div>
          {/* Only for games whose server has the concept. Some games' access
              control is a username whitelist, not a shared password. */}
          {game?.supports_server_password && (
            <SecretField
              label="Join password (optional)"
              value={serverPassword}
              onChange={setServerPassword}
              placeholder="Leave blank for an open world"
              hint="Players enter this when they join. You can change it later, but only while the world is off."
              emptyHint={
                <>
                  <strong>
                    Without a password, anyone who knows the address can join.
                  </strong>{" "}
                  The world is unlisted, not private.
                </>
              }
            />
          )}

          {settingsProblems.map((p) => (
            <div key={p} className="hint error small">
              {p}
            </div>
          ))}

          {/* Only meaningful for a game whose worlds each need a port; the
              range is the platform-wide ceiling on how many can exist. */}
          {game?.ports_available !== null &&
            game?.ports_available !== undefined && (
              <div className="hint muted small">
                {game.ports_available === 0
                  ? `No ${game.display_name} ports are free — delete a world before creating another.`
                  : `${game.ports_available} ${game.display_name} port${
                      game.ports_available === 1 ? "" : "s"
                    } left on this server.`}
              </div>
            )}

          <div className="field-label muted small">
            Starting content (optional)
          </div>
          <div className="source-tabs">
            <button
              type="button"
              className={`tab ${source === "empty" ? "tab-active" : ""}`}
              onClick={() => setSource("empty")}
            >
              Empty world
            </button>
            <button
              type="button"
              className={`tab ${source === "upload" ? "tab-active" : ""}`}
              onClick={() => setSource("upload")}
            >
              Upload file
            </button>
            <button
              type="button"
              className={`tab ${source === "drive" ? "tab-active" : ""}`}
              onClick={() => setSource("drive")}
            >
              From Google Drive
            </button>
          </div>

          {source === "empty" ? (
            <span className="hint muted">
              A fresh world will be generated the first time you turn it on.
            </span>
          ) : source === "upload" ? (
            <label>
              World archive
              <input
                type="file"
                accept=".zip,.tar,.tar.gz,.tgz,application/zip,application/gzip,application/x-tar"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
              <span className="hint muted">
                {mod.uploadHelp ?? (
                  <>
                    Upload a <code>.zip</code> or <code>.tar.gz</code> of your
                    server's data folder. A backup downloaded from Drive can be
                    re-uploaded here as-is.
                  </>
                )}
              </span>
            </label>
          ) : !driveLinked ? (
            <div className="notice">
              Link your Google Drive to restore from a previous backup.{" "}
              <a href={api.driveLinkUrl()}>Link now →</a>
            </div>
          ) : backups === null || driveFiles === null ? (
            <div className="muted small">Loading your backups…</div>
          ) : gameBackups.length === 0 && gameFiles.length === 0 ? (
            <div className="muted small">
              No {game?.display_name ?? "matching"} backups in your Drive.
              Backups of another game can&apos;t seed this world — switch the
              game above to reach them.
            </div>
          ) : (
            <label>
              Backup to restore
              <select
                value={currentChoice(selectedBackup, selectedDriveFile)}
                onChange={(e) => {
                  const choice = decodeChoice(e.target.value);
                  setSelectedBackup(choice?.kind === "backup" ? choice.id : "");
                  setSelectedDriveFile(
                    choice?.kind === "drive" ? choice.id : "",
                  );
                }}
              >
                <option value="">Select a backup…</option>
                {/* Ungrouped: with the list already narrowed to the game
                    picked above, a "Your worlds" heading over all of it names
                    nothing. The deleted-worlds group stays -- those are
                    archives with no backup row left, which is worth saying. */}
                {gameBackups.map((b) => (
                  <option key={b.id} value={`backup:${b.id}`}>
                    {backupLabel(b)}
                  </option>
                ))}
                {gameFiles.length > 0 && (
                  <optgroup label="From deleted worlds">
                    {gameFiles.map((f) => (
                      <option
                        key={f.drive_file_id}
                        value={`drive:${f.drive_file_id}`}
                      >
                        {driveLabel(f)}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </label>
          )}

          {error && <div className="error-banner">{error}</div>}
          {busy && progress && !confirmOpen && (
            <div className="inline-progress">
              <span className="spinner spinner-sm" />
              <span className="muted small">{progress}</span>
            </div>
          )}
          <div className="modal-actions">
            <button
              type="button"
              className="btn btn-ghost"
              disabled={busy}
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={busy || settingsProblems.length > 0}
            >
              {busy ? "Creating…" : "Create world"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
