import { describe, expect, it, vi } from "vitest";
import {
  errorKey,
  loadSeen,
  onOpenWorldError,
  openWorldError,
  saveSeen,
} from "./worldErrors";

describe("errorKey", () => {
  it("differs for a new failure of the same world", () => {
    const first = errorKey({ id: "w1", last_error_at: "2026-01-01T00:00:00Z" });
    const again = errorKey({ id: "w1", last_error_at: "2026-01-02T00:00:00Z" });
    expect(first).not.toBe(again);
  });
});

describe("seen errors", () => {
  it("survives a reload", () => {
    saveSeen(new Set(["a", "b"]), ["a", "b"]);
    expect(loadSeen()).toEqual(new Set(["a", "b"]));
  });

  it("drops errors that no longer exist, so it cannot grow", () => {
    saveSeen(new Set(["old", "live"]), ["live", "new"]);
    expect(loadSeen()).toEqual(new Set(["live"]));
  });

  it("starts empty when nothing or garbage is stored", () => {
    expect(loadSeen()).toEqual(new Set());
    localStorage.setItem("gp.seenWorldErrors", "{not json");
    expect(loadSeen()).toEqual(new Set());
  });

  it("does not throw when storage refuses the write", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(() => saveSeen(new Set(["a"]), ["a"])).not.toThrow();
  });
});

describe("open events", () => {
  it("reaches a listener until it unsubscribes", () => {
    const handler = vi.fn();
    const off = onOpenWorldError(handler);
    openWorldError("w1");
    off();
    openWorldError("w2");
    expect(handler).toHaveBeenCalledExactlyOnceWith("w1");
  });
});
