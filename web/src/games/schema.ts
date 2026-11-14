import type { GameInfo, GameSettings, JsonSchema } from "../types";

/** One field of a game's settings form, flattened from its JSON schema. */
export interface SettingsField {
  key: string;
  label: string;
  description?: string;
  kind: "enum" | "string" | "number" | "boolean";
  options?: string[];
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  default?: unknown;
}

function resolve(schema: JsonSchema, root: JsonSchema): JsonSchema {
  // pydantic writes an enum-typed field as {"$ref": "#/$defs/X"}, sometimes
  // inside allOf, with the field's own title and default beside it.
  const ref = schema.$ref ?? schema.allOf?.find((s) => s.$ref)?.$ref;
  if (!ref) return schema;
  const name = ref.split("/").pop() ?? "";
  return { ...(root.$defs?.[name] ?? {}), ...schema, $ref: undefined };
}

/** The settings form for a game, in the order the API declares the fields. */
export function settingsFields(game: GameInfo): SettingsField[] {
  const root = game.settings_schema;
  return Object.entries(root.properties ?? {}).map(([key, raw]) => {
    const s = resolve(raw, root);
    const kind: SettingsField["kind"] = s.enum
      ? "enum"
      : s.type === "boolean"
        ? "boolean"
        : s.type === "integer" || s.type === "number"
          ? "number"
          : "string";
    return {
      key,
      label: s.title ?? key,
      description: s.description,
      kind,
      options: s.enum?.map(String),
      maxLength: s.maxLength,
      minimum: s.minimum,
      maximum: s.maximum,
      default: s.default,
    };
  });
}

/** Settings for a new world: every field at its schema default. */
export function defaultSettings(game: GameInfo): GameSettings {
  const out: GameSettings = {};
  for (const f of settingsFields(game)) {
    if (f.default !== undefined) out[f.key] = f.default;
  }
  return out;
}

/** Keys whose value differs between two settings objects. */
export function changedKeys(a: GameSettings, b: GameSettings): string[] {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  return [...keys].filter((k) => (a[k] ?? "") !== (b[k] ?? ""));
}
