import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { hasGeolocationGrant, setGeolocationGrant } from "./geolocation_grant.ts";
import { getGeolocation, initialFrameSrc, openExternal } from "./haku_ui_embed.tsx";

describe("initialFrameSrc", () => {
  it("pins the origin and carries only the console hash into the frame's fragment", () => {
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#/runs")).toBe(
      "https://haku-ui.allegedly.works/#/runs"
    );
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#/garden/notes%2Ffoo.md")).toBe(
      "https://haku-ui.allegedly.works/#/garden/notes%2Ffoo.md"
    );
  });

  it("falls back to the bare uiUrl when the hash is absent or not a valid route path", () => {
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "")).toBe("https://haku-ui.allegedly.works/");
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#runs")).toBe("https://haku-ui.allegedly.works/");
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#https://evil.example")).toBe(
      "https://haku-ui.allegedly.works/"
    );
    expect(initialFrameSrc("https://haku-ui.allegedly.works/", "#//evil.example/x")).toBe(
      "https://haku-ui.allegedly.works/"
    );
  });
});

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

describe("getGeolocation", () => {
  afterEach(() => {
    // jsdom has no navigator.geolocation; each test installs its own stub, so clear it.
    Reflect.deleteProperty(navigator, "geolocation");
  });

  function stubGeolocation(impl: Geolocation["getCurrentPosition"]) {
    const getCurrentPosition = vi.fn(impl);
    Object.defineProperty(navigator, "geolocation", {
      configurable: true,
      value: { getCurrentPosition } as unknown as Geolocation,
    });
    return getCurrentPosition;
  }

  it("flattens a GeolocationPosition into a plain, cloneable object", async () => {
    stubGeolocation((success) =>
      success({
        coords: {
          latitude: 37.77,
          longitude: -122.41,
          accuracy: 12,
          altitude: null,
          altitudeAccuracy: null,
          heading: null,
          speed: null,
        },
        timestamp: 1_700_000_000_000,
      } as unknown as GeolocationPosition)
    );
    const r = await getGeolocation();
    expect(r).toEqual({
      ok: true,
      position: {
        latitude: 37.77,
        longitude: -122.41,
        accuracy: 12,
        altitude: null,
        altitudeAccuracy: null,
        heading: null,
        speed: null,
        timestamp: 1_700_000_000_000,
      },
    });
  });

  it("passes options through and surfaces a browser error as a {code, message} result", async () => {
    const getCurrentPosition = stubGeolocation((_success, error) =>
      error?.({ code: 1, message: "User denied Geolocation" } as unknown as GeolocationPositionError)
    );
    const opts = { enableHighAccuracy: true, timeout: 5000 };
    const r = await getGeolocation(opts);
    expect(r).toEqual({ ok: false, code: 1, message: "User denied Geolocation" });
    expect(getCurrentPosition).toHaveBeenCalledWith(expect.any(Function), expect.any(Function), opts);
  });

  it("resolves PERMISSION_DENIED when the browser has no geolocation API", async () => {
    expect("geolocation" in navigator).toBe(false);
    expect(await getGeolocation()).toEqual({
      ok: false,
      code: 1,
      message: "Geolocation is unavailable in this browser.",
    });
  });
});

describe("geolocation grant (standing consent, shell localStorage)", () => {
  beforeEach(() => localStorage.clear());

  it("is absent until granted, and cleared on withdraw", () => {
    expect(hasGeolocationGrant()).toBe(false);
    setGeolocationGrant(true);
    expect(hasGeolocationGrant()).toBe(true);
    setGeolocationGrant(false);
    expect(hasGeolocationGrant()).toBe(false);
  });
});
