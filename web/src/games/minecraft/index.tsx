import type { GameModule } from "../types";
import AdminContentSection from "./AdminContentSection";
import WorldContentSection from "./WorldContentSection";

// Parse a plain release like "1.20.4"; null for LATEST/SNAPSHOT/weekly builds.
function releaseTuple(v: string): number[] | null {
  const parts = v.trim().split(".");
  if (parts.length === 0 || !parts.every((p) => /^\d+$/.test(p))) return null;
  return parts.map(Number);
}

function isDowngrade(oldV: string, newV: string): boolean {
  const a = releaseTuple(oldV);
  const b = releaseTuple(newV);
  if (!a || !b) return false;
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    if (y < x) return true;
    if (y > x) return false;
  }
  return false;
}

const minecraft: GameModule = {
  // Box art puts the logo at the top; anchor there so neither a wide card's
  // crop nor the hover zoom cuts it off.
  artPosition: "center top",
  tags: (w) => [
    ...(w.settings.server_type
      ? [{ text: String(w.settings.server_type), kind: "type" as const }]
      : []),
    ...(w.settings.version
      ? [{ text: String(w.settings.version), kind: "version" as const }]
      : []),
  ],
  settingsRisks: (from, to) => {
    const out: string[] = [];
    const [oldV, newV] = [String(from.version ?? ""), String(to.version ?? "")];
    if (oldV !== newV && isDowngrade(oldV, newV)) {
      out.push(
        `Downgrading from ${oldV} to ${newV} can corrupt or fail to load your existing world data. Back up first and proceed only if you're sure.`,
      );
    }
    if (from.server_type !== to.server_type) {
      out.push(
        `Switching from ${String(from.server_type)} to ${String(to.server_type)} may break installed mods/plugins and world data.`,
      );
    }
    return out;
  },
  // mc-router holds the player's connection while the world boots.
  wakeNote: "The first join just takes a few extra seconds.",
  uploadHelp: (
    <>
      Upload a <code>.zip</code> or <code>.tar.gz</code> of your server's{" "}
      <strong>data folder</strong> — the folder that holds your{" "}
      <code>world/</code> directory (and, if you have them,{" "}
      <code>server.properties</code>, <code>mods/</code>, <code>plugins/</code>,
      configs). It's fine if everything sits inside one top-level folder in the
      archive (the usual result of zipping a folder) — its name doesn't matter.
      This is exactly how the backups you download from Drive are shaped
      (wrapped in a <code>data/</code> folder), so a downloaded backup can be
      re-uploaded here as-is. A single world folder (with <code>level.dat</code>{" "}
      inside) also works and will be placed under <code>world/</code>{" "}
      automatically.
    </>
  ),
  iconHelp: (fromSave) =>
    fromSave
      ? "From the world save. Uploading one replaces it in the Minecraft server list after the next restart."
      : "Shows in the Minecraft server list after the next restart.",
  WorldContent: WorldContentSection,
  adminSections: [
    { id: "content", label: "Add-on catalog", Component: AdminContentSection },
  ],
};

export default minecraft;
