import { describe, expect, it } from "vitest";
import { makeGame } from "../test/fixtures";
import { changedKeys, defaultSettings, settingsFields } from "./schema";

// Shaped like pydantic's output for an adapter's settings model.
const game = makeGame({
  settings_schema: {
    type: "object",
    $defs: {
      Difficulty: {
        title: "Difficulty",
        enum: ["peaceful", "easy", "normal", "hard"],
        type: "string",
      },
    },
    properties: {
      version: { title: "Version", type: "string", default: "1.21.4" },
      difficulty: { $ref: "#/$defs/Difficulty", default: "normal" },
      mode: {
        allOf: [{ $ref: "#/$defs/Difficulty" }],
        title: "Mode",
        description: "Wrapped the way pydantic wraps a field with a title.",
      },
      view_distance: {
        title: "View distance",
        type: "integer",
        minimum: 2,
        maximum: 32,
        default: 10,
      },
      hardcore: { title: "Hardcore", type: "boolean", default: false },
      motd: { type: "string", maxLength: 59 },
    },
  },
});

describe("settingsFields", () => {
  const fields = settingsFields(game);
  const byKey = Object.fromEntries(fields.map((f) => [f.key, f]));

  it("keeps the API's field order", () => {
    expect(fields.map((f) => f.key)).toEqual([
      "version",
      "difficulty",
      "mode",
      "view_distance",
      "hardcore",
      "motd",
    ]);
  });

  it("resolves a $ref enum, keeping the field's own default", () => {
    expect(byKey.difficulty).toMatchObject({
      kind: "enum",
      label: "Difficulty",
      options: ["peaceful", "easy", "normal", "hard"],
      default: "normal",
    });
  });

  it("resolves a $ref inside allOf, keeping the field's own title", () => {
    expect(byKey.mode).toMatchObject({
      kind: "enum",
      label: "Mode",
      description: "Wrapped the way pydantic wraps a field with a title.",
    });
  });

  it("maps types to inputs", () => {
    expect(byKey.version.kind).toBe("string");
    expect(byKey.view_distance).toMatchObject({
      kind: "number",
      minimum: 2,
      maximum: 32,
    });
    expect(byKey.hardcore.kind).toBe("boolean");
  });

  it("falls back to the key when there is no title", () => {
    expect(byKey.motd).toMatchObject({ label: "motd", maxLength: 59 });
  });

  it("is empty for a game with no settings", () => {
    expect(settingsFields(makeGame())).toEqual([]);
  });
});

describe("defaultSettings", () => {
  // `false` is a default too; only a missing one is left out.
  it("takes every default, falsy ones included", () => {
    expect(defaultSettings(game)).toEqual({
      version: "1.21.4",
      difficulty: "normal",
      view_distance: 10,
      hardcore: false,
    });
  });
});

describe("changedKeys", () => {
  it("lists keys whose value differs", () => {
    expect(changedKeys({ a: 1, b: 2 }, { a: 1, b: 3, c: 4 })).toEqual([
      "b",
      "c",
    ]);
  });

  // A field never set and one cleared to "" read the same in the form.
  it("treats missing and empty as equal", () => {
    expect(changedKeys({ motd: "" }, {})).toEqual([]);
  });
});
