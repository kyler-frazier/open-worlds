import { settingsFields, type SettingsField } from "./schema";
import type { SettingsFieldsProps } from "./types";

/**
 * A game's own settings, rendered from the JSON schema the API publishes.
 *
 * The one settings form for every game: a select for an enum, a text or number
 * input otherwise, with the schema's title as the label and its description as
 * the hint. A new game's settings appear here without any web code.
 */
export default function SettingsFields({
  game,
  settings,
  onChange,
  specLocked,
}: SettingsFieldsProps) {
  const fields = settingsFields(game);
  if (fields.length === 0) return null;
  const set = (key: string, value: unknown) =>
    onChange({ ...settings, [key]: value });
  const field = (f: SettingsField) => {
    const disabled = specLocked && game.spec_fields.includes(f.key);
    const value = settings[f.key] ?? f.default ?? "";
    return (
      <label key={f.key}>
        {f.kind !== "boolean" && f.label}
        {f.kind === "enum" ? (
          <select
            value={String(value)}
            disabled={disabled}
            onChange={(e) => set(f.key, e.target.value)}
          >
            {(f.options ?? []).map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        ) : f.kind === "boolean" ? (
          <span>
            <input
              type="checkbox"
              checked={Boolean(value)}
              disabled={disabled}
              onChange={(e) => set(f.key, e.target.checked)}
            />{" "}
            {f.label}
          </span>
        ) : (
          <input
            type={f.kind === "number" ? "number" : "text"}
            value={String(value)}
            maxLength={f.maxLength}
            min={f.minimum}
            max={f.maximum}
            placeholder={f.default ? String(f.default) : undefined}
            disabled={disabled}
            onChange={(e) =>
              set(
                f.key,
                f.kind === "number" ? Number(e.target.value) : e.target.value,
              )
            }
          />
        )}
        {f.description && (
          <span className="hint muted small">{f.description}</span>
        )}
      </label>
    );
  };
  // Free text gets a row of its own; everything short shares one.
  const wide = (f: SettingsField) =>
    f.kind === "string" && (f.maxLength === undefined || f.maxLength > 64);
  const short = fields.filter((f) => !wide(f));
  return (
    <>
      {short.length > 0 && <div className="form-row">{short.map(field)}</div>}
      {fields.filter(wide).map(field)}
    </>
  );
}
