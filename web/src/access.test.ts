import { describe, expect, it } from "vitest";
import {
  canChangeEgress,
  canManageBackups,
  canManageSettings,
  isStrictOwner,
} from "./access";
import { makeLimits, makeMe, makeWorld } from "./test/fixtures";
import type { AccessLevel } from "./types";

const at = (access_level: AccessLevel) => makeWorld({ access_level });

describe("canManageSettings", () => {
  it.each([
    ["OPERATOR", false],
    ["MANAGER", true],
    ["OWNER", true],
  ] as const)("%s -> %s", (level, expected) => {
    expect(canManageSettings(at(level))).toBe(expected);
  });
});

describe("canManageBackups", () => {
  it.each([
    ["OPERATOR", false],
    ["MANAGER", false],
    ["OWNER", true],
  ] as const)("%s -> %s", (level, expected) => {
    expect(canManageBackups(at(level))).toBe(expected);
  });
});

describe("isStrictOwner", () => {
  it("is the owner", () => {
    expect(isStrictOwner(makeWorld(), makeMe())).toBe(true);
  });

  // The server answers OWNER on the ladder for admins; this must not.
  it("excludes an admin who is not the owner", () => {
    const me = makeMe({ id: "u-admin", is_admin: true, role: "ADMIN" });
    expect(isStrictOwner(at("OWNER"), me)).toBe(false);
  });

  it("is false when signed out", () => {
    expect(isStrictOwner(makeWorld(), null)).toBe(false);
  });
});

describe("canChangeEgress", () => {
  const guarded = (level: AccessLevel) =>
    makeWorld({ egress_guarded: true, access_level: level });
  const admin = makeMe({ is_admin: true });

  it("is nobody by default", () => {
    expect(canChangeEgress(guarded("OWNER"), admin, makeLimits())).toBe(false);
  });

  it("is admins only when set to admin", () => {
    const limits = makeLimits({ egress_override_actors: "admin" });
    expect(canChangeEgress(guarded("OWNER"), admin, limits)).toBe(true);
    expect(canChangeEgress(guarded("OWNER"), makeMe(), limits)).toBe(false);
  });

  it("is managers and up when set to owner_and_manager", () => {
    const limits = makeLimits({ egress_override_actors: "owner_and_manager" });
    expect(canChangeEgress(guarded("OWNER"), makeMe(), limits)).toBe(true);
    expect(canChangeEgress(guarded("MANAGER"), makeMe(), limits)).toBe(true);
    expect(canChangeEgress(guarded("OPERATOR"), makeMe(), limits)).toBe(false);
  });

  it("is never for an unguarded world", () => {
    const limits = makeLimits({ egress_override_actors: "admin" });
    expect(canChangeEgress(makeWorld(), admin, limits)).toBe(false);
  });

  it("is never before limits or the user have loaded", () => {
    const limits = makeLimits({ egress_override_actors: "admin" });
    expect(canChangeEgress(guarded("OWNER"), admin, null)).toBe(false);
    expect(canChangeEgress(guarded("OWNER"), null, limits)).toBe(false);
  });
});
