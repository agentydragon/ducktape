import { afterEach, describe, expect, it, vi } from "vitest";

import { openExternal } from "./open_external";

describe("openExternal", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("uses the blank-tab handle as the HTTPS popup signal, then severs opener before navigation", () => {
    const opened = { opener: window, location: { replace: vi.fn() } };
    vi.spyOn(window, "open").mockReturnValue(opened as unknown as Window);

    expect(openExternal("https://example.com/path")).toBe(true);

    expect(window.open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(opened.opener).toBeNull();
    expect(opened.location.replace).toHaveBeenCalledWith("https://example.com/path");
  });

  it("reports blocked HTTPS tabs when the blank tab returns no handle", () => {
    vi.spyOn(window, "open").mockReturnValue(null);

    expect(openExternal("https://example.com/path")).toBe(false);
  });

  it("opens mailto directly without leaving an about:blank tab", () => {
    const opened = { opener: window };
    vi.spyOn(window, "open").mockReturnValue(opened as unknown as Window);

    expect(openExternal("mailto:ops@allegedly.works")).toBe(true);

    expect(window.open).toHaveBeenCalledWith("mailto:ops@allegedly.works", "_blank");
    expect(opened.opener).toBeNull();
  });
});
