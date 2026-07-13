// Browser-geolocation glue for the shell. Only the trusted top-level origin reads location
// (the iframe has no `allow="geolocation"`); the shell exposes it to the iframe over the
// bridge, gated by the standing consent grant. Two shapes: a one-shot read
// (`getGeolocation`, for `requestGeolocation`) and a live watch (`GeolocationWatcher`, for
// `startGeolocationWatch`/`stopGeolocationWatch`). See docs/containment.md → geolocation.

import type { GeolocationOptions, GeoPosition } from "@haku/console-bridge/protocol";

// GeolocationPositionError.code for "no geolocation API" — matches the browser's own error
// taxonomy (1 PERMISSION_DENIED, 2 POSITION_UNAVAILABLE, 3 TIMEOUT) so the iframe can treat
// every failure uniformly, whatever its source. Also used for operator decline/withdraw.
export const GEO_PERMISSION_DENIED = 1;

// Flatten the browser's `GeolocationPosition`/`GeolocationCoordinates` (not reliably
// cloneable across postMessage) into a plain object; `altitude`/`heading`/`speed` are null
// when the device can't supply them.
export function flattenPosition({ coords, timestamp }: GeolocationPosition): GeoPosition {
  return {
    latitude: coords.latitude,
    longitude: coords.longitude,
    accuracy: coords.accuracy,
    altitude: coords.altitude,
    altitudeAccuracy: coords.altitudeAccuracy,
    heading: coords.heading,
    speed: coords.speed,
    timestamp,
  };
}

export type GeoResult = { ok: true; position: GeoPosition } | { ok: false; code: number; message: string };

// One-shot read of the shell origin's location. Only after the operator approved on trusted
// chrome; the browser may still surface its own native prompt on first use. Never throws:
// resolves to a discriminated result so the caller reports it over the bridge either way.
export function getGeolocation(options?: GeolocationOptions): Promise<GeoResult> {
  return new Promise((resolve) => {
    if (!("geolocation" in navigator)) {
      resolve({ ok: false, code: GEO_PERMISSION_DENIED, message: "Geolocation is unavailable in this browser." });
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ ok: true, position: flattenPosition(pos) }),
      (err) => resolve({ ok: false, code: err.code, message: err.message }),
      options
    );
  });
}

// One streamed fix (or error) from a live watch — the payload the shell relays to the iframe.
export type WatchEmit = { ok: true; position: GeoPosition } | { ok: false; code: number; message: string };

// Manages shell-side `navigator.geolocation.watchPosition` streams keyed by the bridge's
// per-watch id (the iframe's correlation id). Each active watch pushes fixes/errors to
// `emit(id, ...)` until stopped. The shell (not the iframe) holds every watch, so a
// prompt-injected Haku can neither start one without the consent grant nor keep one the
// operator has stopped — `stopAll` is the Location panel's kill switch.
export class GeolocationWatcher {
  private readonly watches = new Map<string, number>(); // bridge watch id → browser watch id

  constructor(private readonly emit: (id: string, e: WatchEmit) => void) {}

  // Idempotent per id: a duplicate start for a live id is ignored (the browser watch stays).
  start(id: string, options?: GeolocationOptions): void {
    if (this.watches.has(id)) return;
    if (!("geolocation" in navigator)) {
      this.emit(id, { ok: false, code: GEO_PERMISSION_DENIED, message: "Geolocation is unavailable in this browser." });
      return;
    }
    const browserId = navigator.geolocation.watchPosition(
      (pos) => this.emit(id, { ok: true, position: flattenPosition(pos) }),
      (err) => this.emit(id, { ok: false, code: err.code, message: err.message }),
      options
    );
    this.watches.set(id, browserId);
  }

  stop(id: string): void {
    const browserId = this.watches.get(id);
    if (browserId === undefined) return;
    navigator.geolocation.clearWatch(browserId);
    this.watches.delete(id);
  }

  // Stop every live watch (the withdraw / unmount kill switch). Returns the stopped ids so
  // the caller can send each a terminal message over the bridge.
  stopAll(): string[] {
    const ids = [...this.watches.keys()];
    for (const browserId of this.watches.values()) navigator.geolocation.clearWatch(browserId);
    this.watches.clear();
    return ids;
  }

  get activeCount(): number {
    return this.watches.size;
  }
}
