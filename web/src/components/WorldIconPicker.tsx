import { useRef, useState } from "react";
import { ApiError, api } from "../api";
import { confirmDialog } from "../confirmService";
import type { IconFile, World } from "../types";
import { gameModule } from "../games";

// What the server keeps: exactly this size, as a PNG. Resizing here means any
// picture works, rather than asking people to make a 64px PNG themselves.
const ICON_PX = 64;

/** Centre-crop any image the browser can decode to a 64x64 PNG. */
async function toIconPng(file: File): Promise<Blob> {
  const bitmap = await createImageBitmap(file);
  try {
    const side = Math.min(bitmap.width, bitmap.height);
    const canvas = document.createElement("canvas");
    canvas.width = ICON_PX;
    canvas.height = ICON_PX;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Your browser could not resize the image.");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(
      bitmap,
      (bitmap.width - side) / 2,
      (bitmap.height - side) / 2,
      side,
      side,
      0,
      0,
      ICON_PX,
      ICON_PX,
    );
    return await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob(
        (b) =>
          b ? resolve(b) : reject(new Error("Could not encode the image.")),
        "image/png",
      ),
    );
  } finally {
    bitmap.close();
  }
}

interface Props {
  world: World;
  /** Whether the caller may change it. Otherwise the tile is display-only. */
  editable: boolean;
  /** Called after an upload or removal lands, so the dashboard refreshes. */
  onChanged: () => void;
}

/**
 * The world icon as one clickable tile: the icon (or an empty square) with a
 * pencil over it. It saves on pick rather than with the form's Save, because a
 * file does not fit the form's list-of-field-changes confirmation.
 */
export default function WorldIconPicker({ world, editable, onChanged }: Props) {
  const mod = gameModule(world.game);
  const input = useRef<HTMLInputElement>(null);
  // Local, so the tile updates from the upload's answer without waiting for
  // the dashboard to reload and hand the modal a fresh world.
  const [version, setVersion] = useState(world.icon_version);
  const [custom, setCustom] = useState(world.icon_is_custom);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = async (fn: () => Promise<World>) => {
    setBusy(true);
    setError(null);
    try {
      const updated = await fn();
      setVersion(updated.icon_version);
      setCustom(updated.icon_is_custom);
      onChanged();
    } catch (err) {
      setError(
        err instanceof ApiError || err instanceof Error
          ? err.message
          : "Could not update the icon",
      );
    } finally {
      setBusy(false);
    }
  };

  // Clearing deletes every icon file, not just the one on show: a game may fall
  // back from its server icon to the save's icon, so taking only ours would
  // leave an icon up. The dialog shows each file and its path first.
  const clear = async () => {
    let files: IconFile[];
    try {
      files = await api.listWorldIconFiles(world.id);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not read the icon",
      );
      return;
    }
    if (files.length === 0) {
      setVersion(null);
      onChanged();
      return;
    }
    const ok = await confirmDialog({
      title: "Clear icon",
      message:
        files.length > 1
          ? "Both of these files will be deleted:"
          : "This file will be deleted:",
      details: (
        <ul className="icon-clear-list">
          {files.map((f) => (
            <li key={f.kind}>
              <div className="icon-tile icon-tile-sm">
                {f.valid && (
                  <img
                    src={api.worldIconFileUrl(world.id, f.kind, f.version)}
                    alt=""
                  />
                )}
              </div>
              <div>
                <div>
                  {f.kind === "server" ? "Server icon" : "World save icon"}
                  {!f.valid && (
                    <span className="muted"> (not a valid icon)</span>
                  )}
                </div>
                <code className="small">{f.path}</code>
              </div>
            </li>
          ))}
        </ul>
      ),
      confirmLabel: "Clear icon",
      danger: true,
    });
    if (ok) void apply(() => api.clearWorldIcon(world.id));
  };

  const picked = (file: File | undefined) => {
    // Cleared so picking the same file again still fires a change.
    if (input.current) input.current.value = "";
    if (!file) return;
    void apply(async () =>
      api.uploadWorldIcon(world.id, await toIconPng(file)),
    );
  };

  const img =
    version !== null ? (
      <img src={api.worldIconUrl(world.id, version)} alt="" />
    ) : null;

  return (
    <div className="icon-picker">
      <span className="icon-picker-label">Icon</span>
      <div className="icon-picker-row">
        {editable ? (
          <button
            type="button"
            className={`icon-tile ${img ? "" : "icon-tile-empty"}`}
            aria-label={img ? "Change world icon" : "Add a world icon"}
            disabled={busy}
            onClick={() => input.current?.click()}
          >
            {img}
            <span className="icon-tile-pencil" aria-hidden="true">
              <svg viewBox="0 0 24 24" focusable="false">
                <path
                  fill="currentColor"
                  d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zm17.71-10.21a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"
                />
              </svg>
            </span>
          </button>
        ) : (
          <div className={`icon-tile ${img ? "" : "icon-tile-empty"}`}>
            {img}
          </div>
        )}
        <div className="icon-picker-side">
          <span className="hint muted">
            {/* Where the icon shows is the game's answer; one the game
                keeps in the save is already what players see. */}
            {mod.iconHelp?.(!!img && !custom) ??
              "Shown on this dashboard only."}
          </span>
          {editable && img && (
            <button
              type="button"
              className="btn-link-danger"
              disabled={busy}
              onClick={() => void clear()}
            >
              Clear icon
            </button>
          )}
          {busy && <span className="hint muted">Saving…</span>}
        </div>
      </div>
      {error && <div className="error-banner">{error}</div>}
      <input
        ref={input}
        type="file"
        accept="image/*"
        hidden
        onChange={(e) => picked(e.target.files?.[0])}
      />
    </div>
  );
}
