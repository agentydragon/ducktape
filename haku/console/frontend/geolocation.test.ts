import { afterEach, describe, expect, it, vi } from "vitest";

import { GeolocationWatcher, getGeolocation, type WatchEmit } from "./geolocation";

// jsdom has no navigator.geolocation; each test installs its own stub, so clear it after.
afterEach(() => Reflect.deleteProperty(navigator, "geolocation"));

function positionAt(latitude: number, longitude: number): GeolocationPosition {
  return {
    coords: { latitude, longitude, accuracy: 10, altitude: null, altitudeAccuracy: null, heading: null, speed: null },
    timestamp: 1_700_000_000_000,
  } as unknown as GeolocationPosition;
}

describe("getGeolocation", () => {
  function stubGetCurrentPosition(impl: Geolocation["getCurrentPosition"]) {
    const getCurrentPosition = vi.fn(impl);
    Object.defineProperty(navigator, "geolocation", {
      configurable: true,
      value: { getCurrentPosition } as unknown as Geolocation,
    });
    return getCurrentPosition;
  }

  it("flattens a GeolocationPosition into a plain, cloneable object", async () => {
    stubGetCurrentPosition((success) => success(positionAt(37.77, -122.41)));
    expect(await getGeolocation()).toEqual({
      ok: true,
      position: {
        latitude: 37.77,
        longitude: -122.41,
        accuracy: 10,
        altitude: null,
        altitudeAccuracy: null,
        heading: null,
        speed: null,
        timestamp: 1_700_000_000_000,
      },
    });
  });

  it("passes options through and surfaces a browser error as a {code, message} result", async () => {
    const getCurrentPosition = stubGetCurrentPosition((_success, error) =>
      error?.({ code: 1, message: "User denied Geolocation" } as unknown as GeolocationPositionError)
    );
    const opts = { enableHighAccuracy: true, timeout: 5000 };
    expect(await getGeolocation(opts)).toEqual({ ok: false, code: 1, message: "User denied Geolocation" });
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

describe("GeolocationWatcher", () => {
  // Stub watchPosition to hand back an incrementing id and stash each watch's callbacks, so a
  // test can drive fixes/errors into a live watch; clearWatch removes it.
  function stubWatch() {
    let nextId = 0;
    const watches = new Map<number, { success: PositionCallback; error?: PositionErrorCallback | null }>();
    const watchPosition = vi.fn((success: PositionCallback, error?: PositionErrorCallback | null) => {
      const id = ++nextId;
      watches.set(id, { success, error });
      return id;
    });
    const clearWatch = vi.fn((id: number) => void watches.delete(id));
    Object.defineProperty(navigator, "geolocation", {
      configurable: true,
      value: { watchPosition, clearWatch } as unknown as Geolocation,
    });
    return { watchPosition, clearWatch, watches };
  }

  function collector() {
    const emits: [string, WatchEmit][] = [];
    const watcher = new GeolocationWatcher((id, e) => void emits.push([id, e]));
    return { emits, watcher };
  }

  it("relays every fix as an emit tagged with the bridge watch id", () => {
    const { watches } = stubWatch();
    const { emits, watcher } = collector();
    watcher.start("w1");
    expect(watcher.activeCount).toBe(1);
    const { success } = [...watches.values()][0];
    success(positionAt(1, 2));
    success(positionAt(3, 4));
    expect(emits.map(([id, e]) => [id, e.ok, e.ok ? [e.position.latitude, e.position.longitude] : e.code])).toEqual([
      ["w1", true, [1, 2]],
      ["w1", true, [3, 4]],
    ]);
  });

  it("relays a watch error as an ok:false emit, passing options through", () => {
    const { watchPosition, watches } = stubWatch();
    const { emits, watcher } = collector();
    const opts = { enableHighAccuracy: false, maximumAge: 30_000 };
    watcher.start("w1", opts);
    expect(watchPosition).toHaveBeenCalledWith(expect.any(Function), expect.any(Function), opts);
    [...watches.values()][0].error?.({
      code: 2,
      message: "position unavailable",
    } as unknown as GeolocationPositionError);
    expect(emits).toEqual([["w1", { ok: false, code: 2, message: "position unavailable" }]]);
  });

  it("is idempotent per id — a duplicate start does not open a second browser watch", () => {
    const { watchPosition } = stubWatch();
    const { watcher } = collector();
    watcher.start("w1");
    watcher.start("w1");
    expect(watchPosition).toHaveBeenCalledTimes(1);
    expect(watcher.activeCount).toBe(1);
  });

  it("stop clears the browser watch for that id", () => {
    const { clearWatch } = stubWatch();
    const { watcher } = collector();
    watcher.start("w1");
    watcher.stop("w1");
    expect(clearWatch).toHaveBeenCalledWith(1);
    expect(watcher.activeCount).toBe(0);
  });

  it("stopAll clears every live watch and returns their ids", () => {
    const { clearWatch } = stubWatch();
    const { watcher } = collector();
    watcher.start("a");
    watcher.start("b");
    expect(watcher.activeCount).toBe(2);
    expect(watcher.stopAll().sort()).toEqual(["a", "b"]);
    expect(clearWatch).toHaveBeenCalledTimes(2);
    expect(watcher.activeCount).toBe(0);
  });

  it("emits PERMISSION_DENIED (and opens no watch) when the browser has no geolocation API", () => {
    const { emits, watcher } = collector();
    watcher.start("w1");
    expect(emits).toEqual([["w1", { ok: false, code: 1, message: "Geolocation is unavailable in this browser." }]]);
    expect(watcher.activeCount).toBe(0);
  });
});
