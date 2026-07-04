// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { clearBlobCache, getBlob, hasBlob, setBlob } from "./blob_cache.ts";

afterEach(() => {
  clearBlobCache();
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("blob cache", () => {
  it("persists a blob to localStorage and reads it back", () => {
    setBlob("sha1", "hello");
    expect(localStorage.getItem("haku-blob:sha1")).toBe("hello");
    expect(getBlob("sha1")).toBe("hello");
    expect(hasBlob("sha1")).toBe(true);
  });

  it("reads a localStorage-only entry (a prior session's) on a memory miss", () => {
    // Written directly to storage, never through this session's memory tier — the reload case.
    localStorage.setItem("haku-blob:sha2", "from-disk");
    expect(getBlob("sha2")).toBe("from-disk");
  });

  it("returns undefined for an uncached sha", () => {
    expect(getBlob("nope")).toBeUndefined();
    expect(hasBlob("nope")).toBe(false);
  });

  it("keeps the blob in memory and logs (never swallows) when localStorage.setItem throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(() => setBlob("sha3", "big")).not.toThrow();
    expect(getBlob("sha3")).toBe("big"); // memory tier still serves it
    expect(warn).toHaveBeenCalled(); // the failure is surfaced, not silent
  });

  it("clearBlobCache removes only our keys, leaving unrelated localStorage entries", () => {
    setBlob("sha4", "x");
    localStorage.setItem("unrelated", "keep");
    clearBlobCache();
    expect(getBlob("sha4")).toBeUndefined();
    expect(localStorage.getItem("haku-blob:sha4")).toBeNull();
    expect(localStorage.getItem("unrelated")).toBe("keep");
  });
});
