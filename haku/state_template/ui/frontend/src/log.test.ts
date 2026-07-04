import { afterEach, describe, expect, it, vi } from "vitest";

import { logger } from "./log.ts";

afterEach(() => vi.restoreAllMocks());

describe("logger", () => {
  it("prefixes the message with the scope and forwards detail to the matching console level", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const err = new Error("boom");
    logger("blob-cache").warn("could not persist", err);
    expect(warn).toHaveBeenCalledWith("[blob-cache] could not persist", err);
  });

  it("routes each level to the same-named console method", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    logger("routes").info("fell back to home");
    expect(info).toHaveBeenCalledWith("[routes] fell back to home");
  });
});
