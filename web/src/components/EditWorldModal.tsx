import { useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiError, api } from "../api";
import { useAuth } from "../auth";
import {
  canChangeEgress,
  canManageBackups,
  canManageSettings,
  isStrictOwner,
} from "../access";
import { confirmSubdomain } from "../confirm";
import { confirmDialog } from "../confirmService";
import type {
  AutoBackupInterval,
  DriveBackup,
  GameInfo,
  GameSettings,
  Limits,
  RestorableBackup,
  World,
} from "../types";
import {
  changedKeys,
  gameModule,
  SettingsFields,
  settingsFields,
} from "../games";
import { useConfirmOpen } from "../useConfirmOpen";
import {
  stageAndCommitDrive,
  stageAndCommitDriveFile,
  stageAndCommitUpload,
} from "../uploadContent";
import BackupsSection from "./BackupsSection";
import WorldIconPicker from "./WorldIconPicker";
import CollaboratorsSection from "./CollaboratorsSection";
import { backupLabel, currentChoice, decodeChoice } from "../backupChoice";
import DriveArchivePicker from "./DriveArchivePicker";
import InfoTooltip from "./InfoTooltip";
import SecretField from "./SecretField";

interface Props {
  world: World;
  driveLinked: boolean;
  onClose: () => void;
  onSaved: () => void;
  onDeleted: () => void;
}

type Tab = "general" | "backups" | "people" | "content" | "replace" | "danger";
const INTERVALS: AutoBackupInterval[] = ["DAILY", "WEEKLY", "MONTHLY"];

interface Change {
  field: string;
  label: string;
  from: string;
  to: string;
}

export default function EditWorldModal({
  world,
  driveLinked,
  onClose,
  onSaved,
  onDeleted,
}: Props) {
  const [tab, setTab] = useState<Tab>("general");
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // Two axes, both from the server, never inferred from owner_id.
  const { me } = useAuth();
  const isOwner = isStrictOwner(world, me);
  const mayManage = canManageSettings(world);
  const mayOwn = canManageBackups(world);
  // What this world's game actually has. Rather than hard-code which game has
  // which fields, ask the API what the game supports and build the form from that.
  const [gameInfo, setGameInfo] = useState<GameInfo | null>(null);
  // Kept alongside gameInfo because the numeric bounds (memory, player cap) are
  // deployment-wide rather than per-game, and the inputs need them.
  const [limits, setLimits] = useState<Limits | null>(null);

  useEffect(() => {
    api
      .getLimits()
      .then((l) => {
        setLimits(l);
        setGameInfo(l.games.find((g) => g.game === world.game) ?? null);
      })
      .catch(() => {
        setLimits(null);
        setGameInfo(null);
      });
  }, [world.game]);

  const mod = gameModule(world.game);
  const WorldContent = mod.WorldContent;
  const hasCatalog =
    (gameInfo?.supports_content_catalog ?? false) && !!WorldContent;
  const hasPassword = gameInfo?.supports_server_password ?? false;

  const [displayName, setDisplayName] = useState(world.display_name);
  // The game's own settings, edited as a whole and sent as the keys that changed.
  const [settings, setSettings] = useState<GameSettings>(world.settings);
  const settingsProblems = mod.settingsProblems?.(settings) ?? [];
  const [maxPlayers, setMaxPlayers] = useState(world.max_players);
  const [memory, setMemory] = useState(world.memory_mb);
  const [serverPassword, setServerPassword] = useState(world.server_password);
  const [autoEnabled, setAutoEnabled] = useState(world.auto_backup_enabled);
  const [autoInterval, setAutoInterval] = useState<AutoBackupInterval>(
    world.auto_backup_interval,
  );
  const [autoMax, setAutoMax] = useState(world.auto_backup_max);
  const [egressLogOnly, setEgressLogOnly] = useState(world.egress_log_only);
  const mayChangeEgress = canChangeEgress(world, me, limits);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Replace-content state (Replace content tab).
  const [replaceSource, setReplaceSource] = useState<"upload" | "drive">(
    "upload",
  );
  const [replaceFile, setReplaceFile] = useState<File | null>(null);
  const [restorable, setRestorable] = useState<RestorableBackup[] | null>(null);
  const [driveFiles, setDriveFiles] = useState<DriveBackup[] | null>(null);
  const [replaceBackup, setReplaceBackup] = useState<string>("");
  // An archive in Drive we no longer track, because its world was deleted.
  // Mutually exclusive with replaceBackup so one source is always unambiguous.
  const [replaceDriveFile, setReplaceDriveFile] = useState<string>("");
  const [replaceBusy, setReplaceBusy] = useState(false);
  const [replaceError, setReplaceError] = useState<string | null>(null);
  const confirmOpen = useConfirmOpen();

  // Memory, player cap, join password and some game settings are baked into
  // the container, so they only take effect when it is recreated -- which
  // happens on the next start of an OFF world. Lock them otherwise (the game
  // says which, in spec_fields). The display name is deliberately *not* in
  // here: it is pure platform metadata for every game.
  const specLocked = world.state !== "OFF";

  useEffect(() => {
    if (replaceSource === "drive" && driveLinked && restorable === null) {
      api
        .listRestorableBackups()
        .then(setRestorable)
        .catch(() => setRestorable([]));
    }
  }, [replaceSource, driveLinked, restorable]);

  // Loaded with the tracked list rather than when the drawer is expanded: a
  // matching archive belongs in the list above, so we cannot wait to be asked
  // before finding out whether there are any.
  useEffect(() => {
    if (replaceSource === "drive" && driveLinked && driveFiles === null) {
      api
        .listDriveBackups()
        .then((all) => setDriveFiles(all.filter((f) => !f.known)))
        .catch(() => setDriveFiles([]));
    }
  }, [replaceSource, driveLinked, driveFiles]);

  const replaceReady =
    replaceSource === "upload"
      ? !!replaceFile
      : !!replaceBackup || !!replaceDriveFile;

  const replaceContent = async () => {
    if (!replaceReady) return;
    if (
      !(await confirmSubdomain(
        world.subdomain,
        `This permanently replaces "${world.display_name}"'s current content with the data you're loading and cannot be undone.`,
        { worldId: world.id, driveLinked },
      ))
    ) {
      return;
    }
    setReplaceBusy(true);
    setReplaceError(null);
    try {
      const label = gameInfo?.display_name ?? "game";
      const committed =
        replaceSource === "upload" && replaceFile
          ? await stageAndCommitUpload(world.id, replaceFile, label)
          : replaceDriveFile
            ? await stageAndCommitDriveFile(world.id, replaceDriveFile, label)
            : await stageAndCommitDrive(world.id, replaceBackup, label);
      if (!committed) {
        setReplaceBusy(false);
        return;
      }
      onSaved();
      onClose();
    } catch (err) {
      setReplaceError(
        err instanceof ApiError ? err.message : "Failed to replace content",
      );
    } finally {
      setReplaceBusy(false);
    }
  };

  // Spec/display changes that require the typed-confirm flow and (for spec
  // fields) the world to be OFF.
  const changes = useMemo<Change[]>(() => {
    const out: Change[] = [];
    if (displayName !== world.display_name)
      out.push({
        field: "display_name",
        label: "Display name",
        from: world.display_name,
        to: displayName,
      });
    if (memory !== world.memory_mb)
      out.push({
        field: "memory_mb",
        label: "Memory",
        from: `${world.memory_mb} MB`,
        to: `${memory} MB`,
      });
    if (maxPlayers !== world.max_players)
      out.push({
        field: "max_players",
        label: "Max players",
        from: String(world.max_players),
        to: String(maxPlayers),
      });
    if (hasPassword && serverPassword !== world.server_password) {
      // Never put the passwords themselves in the confirmation summary — it is a
      // plain-text dialog that may be read over someone's shoulder or in a
      // screen share. Describe the transition instead.
      out.push({
        field: "server_password",
        label: "Join password",
        from: world.server_password ? "set" : "none",
        to: serverPassword ? "changed" : "removed",
      });
    }
    if (gameInfo) {
      const labels = Object.fromEntries(
        settingsFields(gameInfo).map((f) => [f.key, f.label]),
      );
      for (const key of changedKeys(world.settings, settings)) {
        out.push({
          field: `settings.${key}`,
          label: labels[key] ?? key,
          from: String(world.settings[key] ?? "") || "none",
          to: String(settings[key] ?? "") || "none",
        });
      }
    }
    if (mayChangeEgress && egressLogOnly !== world.egress_log_only)
      out.push({
        field: "egress_log_only",
        label: "Unknown network connections",
        from: world.egress_log_only ? "allowed (log only)" : "blocked",
        to: egressLogOnly ? "allowed (log only)" : "blocked",
      });
    return out;
  }, [
    displayName,
    settings,
    gameInfo,
    memory,
    maxPlayers,
    serverPassword,
    hasPassword,
    mayChangeEgress,
    egressLogOnly,
    world,
  ]);

  const gameLabel = gameInfo?.display_name ?? world.game;

  // Both lists are scoped to this world's game. The primary list is this
  // world's own history; other worlds' backups sit behind the drawer.
  const thisWorldBackups = useMemo(
    () => (restorable ?? []).filter((b) => b.world_id === world.id),
    [restorable, world.id],
  );
  const otherWorldBackups = useMemo(
    () =>
      (restorable ?? []).filter(
        (b) => b.game === world.game && b.world_id !== world.id,
      ),
    [restorable, world.game, world.id],
  );
  // Archives with no backup row at all, because the world they came from was
  // deleted. Those written before the Drive layout recorded a game carry null
  // and can never be classified after the fact -- they are kept rather than
  // dropped, because nothing else in the app can reach them and hiding them
  // would strand them permanently. A genuine mismatch is still refused on the
  // way in, so the cost of showing one is an error rather than a bad restore.
  const orphanFiles = useMemo(
    () =>
      (driveFiles ?? []).filter(
        (f) => f.game === world.game || f.game === null,
      ),
    [driveFiles, world.game],
  );
  const noOwnBackups = thisWorldBackups.length === 0;

  const autoChanged =
    autoEnabled !== world.auto_backup_enabled ||
    autoInterval !== world.auto_backup_interval ||
    autoMax !== world.auto_backup_max;

  const risks = useMemo<string[]>(() => {
    const out: string[] = [];
    if (hasPassword && world.server_password && !serverPassword) {
      out.push(
        "Removing the join password opens this world to anyone who knows its address.",
      );
    } else if (
      hasPassword &&
      world.server_password &&
      serverPassword !== world.server_password
    ) {
      out.push(
        "Changing the join password locks out anyone still using the old one.",
      );
    }
    if (maxPlayers < world.max_players) {
      out.push(
        `Lowering the player cap from ${world.max_players} to ${maxPlayers} will keep players out once the world is full.`,
      );
    }
    if (mayChangeEgress && egressLogOnly && !world.egress_log_only) {
      out.push(
        "Allowing unknown network connections lets this world's server reach any address on the internet. Its traffic is still recorded.",
      );
    }
    if (changedKeys(world.settings, settings).length > 0) {
      const risksOf = gameModule(world.game).settingsRisks;
      out.push(...(risksOf?.(world.settings, settings) ?? []));
    }
    return out;
  }, [
    settings,
    maxPlayers,
    serverPassword,
    hasPassword,
    mayChangeEgress,
    egressLogOnly,
    world,
  ]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (changes.length === 0 && !autoChanged) {
      setError("No changes to save.");
      return;
    }

    const body: Record<string, unknown> = {};
    const settingsPatch: GameSettings = {};
    for (const c of changes) {
      if (c.field === "display_name") body.display_name = displayName;
      if (c.field.startsWith("settings.")) {
        const key = c.field.slice("settings.".length);
        settingsPatch[key] = settings[key];
      }
      if (c.field === "memory_mb") body.memory_mb = memory;
      if (c.field === "max_players") body.max_players = maxPlayers;
      // "" is meaningful here — it clears the password and reopens the world —
      // so it is sent, not treated as "unchanged".
      if (c.field === "server_password") body.server_password = serverPassword;
      if (c.field === "egress_log_only") body.egress_log_only = egressLogOnly;
    }
    if (Object.keys(settingsPatch).length > 0) body.settings = settingsPatch;
    if (autoEnabled !== world.auto_backup_enabled)
      body.auto_backup_enabled = autoEnabled;
    if (autoInterval !== world.auto_backup_interval)
      body.auto_backup_interval = autoInterval;
    if (autoMax !== world.auto_backup_max) body.auto_backup_max = autoMax;

    // Spec/display edits go through the shared confirmation dialog (typed-subdomain
    // guard + "back up first" for breaking changes). Auto-backup-only edits are
    // low-risk and save without the extra prompt.
    if (changes.length > 0) {
      const summary = changes
        .map((c) => `  \u2022 ${c.label}: ${c.from} \u2192 ${c.to}`)
        .join("\n");
      let message = `You're about to change:\n\n${summary}`;
      if (risks.length > 0) {
        const riskList = risks.map((r) => `  \u2022 ${r}`).join("\n");
        message += `\n\nHeads up — this change may be breaking:\n${riskList}`;
      }
      const ok = await confirmDialog({
        title: "Save changes",
        message,
        requireText: risks.length > 0 ? world.subdomain : undefined,
        danger: risks.length > 0,
        confirmLabel: "Save changes",
        backup: { worldId: world.id, driveLinked },
      });
      if (!ok) return;
      body.confirm = true;
    }

    setBusy(true);
    try {
      await api.updateWorld(world.id, body);
      onSaved();
      onClose();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to save changes",
      );
    } finally {
      setBusy(false);
    }
  };

  // Deletion lives here rather than on the card so it is reached deliberately.
  // The typed-subdomain confirmation is kept: it is the thing that stops a
  // misclick, and moving the button behind a tab does not replace it.
  const remove = async () => {
    const ok = await confirmSubdomain(
      world.subdomain,
      `Delete "${world.display_name}"? This permanently removes the world and all its data and cannot be undone.`,
      { worldId: world.id, driveLinked },
    );
    if (!ok) return;
    setBusy(true);
    setDeleteError(null);
    try {
      await api.deleteWorld(world.id);
      onDeleted();
      onClose();
    } catch (err) {
      setDeleteError(
        err instanceof ApiError ? err.message : "Could not delete world",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal card modal-wide"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h2>Edit world</h2>
          <div className="modal-head-right">
            <div className="modal-subtitle">
              <span>{world.display_name}</span>
              <span className="muted mono small">{world.subdomain}</span>
            </div>
            <button
              type="button"
              className="modal-close"
              aria-label="Close"
              onClick={onClose}
            >
              ×
            </button>
          </div>
        </div>

        <div className="source-tabs">
          <button
            type="button"
            className={`tab ${tab === "general" ? "tab-active" : ""}`}
            onClick={() => setTab("general")}
          >
            General
          </button>
          <button
            type="button"
            className={`tab ${tab === "backups" ? "tab-active" : ""}`}
            onClick={() => setTab("backups")}
          >
            Backups
          </button>
          {/* A curated catalog needs a trusted registry to curate from. Games
              without one install nothing, so the tab would lead nowhere. */}
          {isOwner && (
            <button
              type="button"
              className={`tab ${tab === "people" ? "tab-active" : ""}`}
              onClick={() => setTab("people")}
            >
              People
            </button>
          )}
          {hasCatalog && mayManage && (
            <button
              type="button"
              className={`tab ${tab === "content" ? "tab-active" : ""}`}
              onClick={() => setTab("content")}
            >
              Add-ons
            </button>
          )}
          {isOwner && (
            <button
              type="button"
              className={`tab ${tab === "replace" ? "tab-active" : ""}`}
              onClick={() => setTab("replace")}
            >
              Replace content
            </button>
          )}
          {/* Deletion gets its own tab rather than a button beside Save.
              It is irreversible and rarely wanted, so it should take a
              deliberate navigation rather than sit under the cursor of
              someone editing a memory limit. */}
          {mayOwn && (
            <button
              type="button"
              className={`tab tab-danger ${tab === "danger" ? "tab-active" : ""}`}
              onClick={() => setTab("danger")}
            >
              Delete
            </button>
          )}
        </div>

        {tab === "general" && (
          <form onSubmit={save} className="form">
            <div className="hint muted small">
              Players connect to <code>{world.connect_address}</code>.
              {world.game_port !== null &&
                " The port is fixed for the life of this world, so the address stays valid."}
            </div>
            <WorldIconPicker
              world={world}
              editable={mayManage}
              onChanged={onSaved}
            />
            <label>
              Display name
              <input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                required
              />
            </label>

            {specLocked && (
              <div className="notice">
                Turn the world off to change settings that are applied when the
                server next starts.
              </div>
            )}

            {gameInfo && (
              <SettingsFields
                game={gameInfo}
                settings={settings}
                onChange={setSettings}
                specLocked={specLocked}
              />
            )}
            {settingsProblems.map((p) => (
              <div key={p} className="hint error small">
                {p}
              </div>
            ))}
            {/* Bounds come from the API, not from constants here: this form used
                to hardcode 32768/64 while the server enforced whatever
                PLATFORM_MAX_MEMORY_MB said, so on a host capped lower you could
                pick a value the form accepted and the API then rejected. */}
            <div className="form-row">
              <label>
                Memory (MB)
                <input
                  type="number"
                  min={limits?.min_memory_mb ?? 1024}
                  max={limits?.max_memory_mb ?? 32768}
                  step={1024}
                  value={memory}
                  disabled={specLocked}
                  onChange={(e) => setMemory(Number(e.target.value))}
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
                  disabled={specLocked}
                  onChange={(e) => setMaxPlayers(Number(e.target.value))}
                />
              </label>
            </div>

            {hasPassword && (
              <SecretField
                label="Join password (optional)"
                value={serverPassword}
                savedValue={world.server_password}
                disabled={specLocked}
                onChange={setServerPassword}
                placeholder="Leave blank for an open world"
                hint="Players enter this when they join."
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

            {mayChangeEgress ? (
              <>
                <div className="field-label muted small">
                  Network protection
                </div>
                {/* The tooltip sits beside the label, not inside it: a click
                    on the icon must not toggle the checkbox. */}
                <div className="toggle-row">
                  <label className="toggle-row">
                    <input
                      type="checkbox"
                      checked={egressLogOnly}
                      onChange={(e) => setEgressLogOnly(e.target.checked)}
                    />
                    <span>Allow unknown network connections</span>
                  </label>
                  <InfoTooltip label="About network protection">
                    Unticked, this world&apos;s server can only reach the
                    services its game needs; anything else is blocked. Ticked,
                    nothing is blocked, but every connection is still recorded
                    and anything that would have been blocked is logged. Applies
                    when the world next starts.
                  </InfoTooltip>
                </div>
              </>
            ) : (
              world.egress_open_exception && (
                <div className="notice">
                  Network protection is relaxed for this world: unknown
                  connections are allowed and logged.
                </div>
              )
            )}

            {error && <div className="error-banner">{error}</div>}
            {!mayManage && (
              <div className="notice">
                You have operator access to this world — you can start and stop
                it, but only a manager or its owner can change these settings.
              </div>
            )}
            <div className="modal-actions">
              {/* "Cancel" when you could have changed something here,
                  "Close" when the tab was only readable. The predicate is
                  whatever gates that tab's primary action (mayManage here,
                  mayOwn on Backups). Tabs rendered only for people who can act
                  on them are always "Cancel". */}
              <button
                type="button"
                className="btn btn-ghost"
                disabled={busy}
                onClick={onClose}
              >
                {mayManage ? "Cancel" : "Close"}
              </button>
              {/* Hidden rather than disabled: there is nothing to save, and a
                  greyed Save invites clicking it to find out why. */}
              {mayManage && (
                <button
                  type="submit"
                  className="btn btn-primary"
                  /* `changes` is the same list the submit handler uses to
                     decide there is nothing to do, so the button and the
                     handler can never disagree about what "changed" means.
                     Matches the Backups tab, which greys out on !autoChanged. */
                  disabled={
                    busy || settingsProblems.length > 0 || changes.length === 0
                  }
                >
                  {busy ? "Saving…" : "Save changes"}
                </button>
              )}
            </div>
          </form>
        )}

        {tab === "backups" && (
          <div className="form">
            {/* The list is visible to anyone who can act on the world --
                knowing whether a world is backed up is useful context
                before touching it. Creating and deleting are not: both
                spend or destroy what is in the OWNER's Drive. */}
            <BackupsSection
              world={world}
              driveLinked={driveLinked}
              onChanged={onSaved}
              canModify={mayOwn}
            />

            <hr className="modal-divider" />
            <div className="field-label muted small">Automatic backups</div>
            {!mayOwn && (
              <div className="notice">
                Only the world&apos;s owner can change this — backups upload to
                their Google Drive.
              </div>
            )}
            {/* Only prompt about Drive if the reader could act on it. It is the
              owner's Drive that matters here, not the viewer's. */}
            {mayOwn && !driveLinked && (
              <div className="notice">
                Link your Google Drive to enable automatic backups.{" "}
                <a href={api.driveLinkUrl()}>Link now →</a>
              </div>
            )}
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={autoEnabled}
                disabled={!driveLinked || !mayOwn}
                onChange={(e) => setAutoEnabled(e.target.checked)}
              />
              <span>Enable automatic backups</span>
            </label>
            <div className="form-row form-row-2">
              <label>
                Cadence
                <select
                  value={autoInterval}
                  disabled={!autoEnabled || !mayOwn}
                  onChange={(e) =>
                    setAutoInterval(e.target.value as AutoBackupInterval)
                  }
                >
                  {INTERVALS.map((i) => (
                    <option key={i} value={i}>
                      {i.charAt(0) + i.slice(1).toLowerCase()}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Keep last (backups)
                <input
                  type="number"
                  min={1}
                  max={100}
                  step={1}
                  value={autoMax}
                  disabled={!autoEnabled || !mayOwn}
                  onChange={(e) => setAutoMax(Number(e.target.value))}
                />
              </label>
            </div>
            <span className="hint muted">
              Only automatic backups count toward the retention limit; manual
              backups are never deleted automatically. Unchanged worlds skip
              their scheduled backup.
            </span>

            {error && <div className="error-banner">{error}</div>}
            <div className="modal-actions">
              <button
                type="button"
                className="btn btn-ghost"
                disabled={busy}
                onClick={onClose}
              >
                {mayOwn ? "Cancel" : "Close"}
              </button>
              {mayOwn && (
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy || !autoChanged}
                  onClick={(e) => void save(e as unknown as FormEvent)}
                >
                  {busy ? "Saving…" : "Save changes"}
                </button>
              )}
            </div>
          </div>
        )}

        {tab === "people" && isOwner && (
          <CollaboratorsSection
            world={world}
            onChanged={onSaved}
            onClose={onClose}
          />
        )}

        {tab === "content" && hasCatalog && mayManage && WorldContent && (
          <WorldContent world={world} onClose={onClose} />
        )}

        {tab === "replace" && isOwner && (
          <div className="form">
            {specLocked ? (
              <div className="notice">
                Turn the world off to replace its content.
              </div>
            ) : (
              <>
                <span className="hint muted">
                  Overwrite this world's current data with an upload or a
                  backup. This can't be undone — you'll be able to back up first
                  when you confirm.
                </span>
                <div className="source-tabs">
                  <button
                    type="button"
                    className={`tab ${replaceSource === "upload" ? "tab-active" : ""}`}
                    onClick={() => setReplaceSource("upload")}
                  >
                    Upload file
                  </button>
                  <button
                    type="button"
                    className={`tab ${replaceSource === "drive" ? "tab-active" : ""}`}
                    onClick={() => setReplaceSource("drive")}
                  >
                    From Google Drive
                  </button>
                </div>

                {replaceSource === "upload" ? (
                  <label>
                    World archive
                    <input
                      type="file"
                      accept=".zip,.tar,.tar.gz,.tgz,application/zip,application/gzip,application/x-tar"
                      onChange={(e) =>
                        setReplaceFile(e.target.files?.[0] ?? null)
                      }
                    />
                  </label>
                ) : !driveLinked ? (
                  <div className="notice">
                    Link your Google Drive to restore from a backup.{" "}
                    <a href={api.driveLinkUrl()}>Link now →</a>
                  </div>
                ) : restorable === null || driveFiles === null ? (
                  <div className="muted small">Loading your backups…</div>
                ) : (
                  <>
                    {/* Rendered even when empty, so the drawer below never
                        reads as the main list. */}
                    <label>
                      Backup to restore
                      {noOwnBackups ? (
                        <div className="muted small">
                          No backups of this world yet. To restore from another{" "}
                          {gameLabel} world, look below.
                        </div>
                      ) : (
                        <select
                          value={currentChoice(replaceBackup, replaceDriveFile)}
                          onChange={(e) => {
                            const choice = decodeChoice(e.target.value);
                            setReplaceBackup(
                              choice?.kind === "backup" ? choice.id : "",
                            );
                            setReplaceDriveFile(
                              choice?.kind === "drive" ? choice.id : "",
                            );
                          }}
                        >
                          <option value="">Select a backup…</option>
                          {/* Ungrouped and unlabelled by world: every row here
                              is this world's own history, so a heading would
                              name the one thing the reader already knows. */}
                          {thisWorldBackups.map((b) => (
                            <option key={b.id} value={`backup:${b.id}`}>
                              {backupLabel(b)}
                            </option>
                          ))}
                        </select>
                      )}
                    </label>
                    <DriveArchivePicker
                      otherBackups={otherWorldBackups}
                      otherFiles={orphanFiles}
                      loading={false}
                      backupId={replaceBackup}
                      driveFileId={replaceDriveFile}
                      onPickBackup={(id) => {
                        setReplaceBackup(id);
                        if (id) setReplaceDriveFile("");
                      }}
                      onPickDriveFile={(id) => {
                        setReplaceDriveFile(id);
                        if (id) setReplaceBackup("");
                      }}
                      startOpen={
                        /* Only worth opening unasked when there is something
                           behind it. Opening onto "Nothing else found" under a
                           field that also says nothing found is two ways of
                           saying the same thing. */
                        noOwnBackups &&
                        (otherWorldBackups.length > 0 || orphanFiles.length > 0)
                      }
                      disabled={replaceBusy}
                    />
                  </>
                )}

                {replaceError && (
                  <div className="error-banner">{replaceError}</div>
                )}
                {replaceBusy && !confirmOpen && (
                  <div className="inline-progress">
                    <span className="spinner spinner-sm" />
                    <span className="muted small">
                      Uploading and validating content…
                    </span>
                  </div>
                )}
                <div className="modal-actions">
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={replaceBusy}
                    onClick={onClose}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={replaceBusy || !replaceReady}
                    onClick={() => void replaceContent()}
                  >
                    {replaceBusy ? "Replacing…" : "Replace content"}
                  </button>
                </div>
              </>
            )}
          </div>
        )}
        {tab === "danger" && mayOwn && (
          <div className="form danger-pane">
            <div className="danger-copy">
              <h3>Delete this world</h3>
              <p className="muted small">
                This removes <strong>{world.display_name}</strong> and all of
                its data — the save, its configuration and any installed
                content. Backups already in Google Drive are not touched, and
                they are the only way back.
              </p>
              <p className="muted small">
                You will be asked to type{" "}
                <code className="mono">{world.subdomain}</code> to confirm.
              </p>
            </div>
            {deleteError && <div className="error-banner">{deleteError}</div>}
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
                type="button"
                className="btn btn-danger"
                disabled={busy}
                onClick={() => void remove()}
              >
                {busy ? "Deleting…" : "Delete world"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
