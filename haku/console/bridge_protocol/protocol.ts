// Wire contract for the console↔iframe postMessage bridge — the single source of truth
// shared by both sides:
//   - the trusted shell (ducktape haku/console: parses Inbound, sends Outbound), and
//   - Haku's UI iframe (haku-state: sends Inbound, listens for Outbound) via client.ts.
//
// Only the MESSAGE SHAPES live here. Shell-only policy — the open-link whitelist, the
// inbound validators, the route-path check — deliberately stays in the shell
// (haku/console/frontend/bridge.ts), PR-gated, so a compromised iframe can't widen it.

// Inbound (iframe → shell). The iframe may only **ask**:
//  - `openLink`: open an external link (the iframe is sandboxed without allow-popups).
//  - `requestLaunch`: start a Haku run with `prompt`; the shell renders its OWN confirm
//    before firing the privileged capability. `id` correlates the `launchResult`.
//  - `requestGeolocation`: one-shot position read (mirrors `getCurrentPosition`). The iframe
//    has no `allow="geolocation"`; the shell reads its own trusted origin's location behind a
//    standing operator grant. `id` correlates the `geolocationResult`.
//  - `startGeolocationWatch` / `stopGeolocationWatch`: a continuous stream (mirrors
//    `watchPosition`/`clearWatch`); the shell holds the live watch and streams each fix as a
//    `geolocationResult` tagged with the same `id` until `stop` (or operator withdrawal).
//  - `routeChanged`: mirror the iframe's hash route into the console's URL fragment. Strictly
//    a validated path (shell-side `isRoutePath`), never a URL.
export type Inbound =
  | { type: "openLink"; url: string }
  | { type: "requestLaunch"; id: string; prompt: string }
  | { type: "requestGeolocation"; id: string; options?: GeolocationOptions }
  | { type: "startGeolocationWatch"; id: string; options?: GeolocationOptions }
  | { type: "stopGeolocationWatch"; id: string }
  | { type: "routeChanged"; path: string };

// Outbound (shell → iframe), so Haku's UI can react to the outcome. A `geolocationResult`
// answers both a one-shot `requestGeolocation` (once) and a `startGeolocationWatch`
// (repeatedly, same `id`, until the watch ends); the iframe correlates by `id`.
export type Outbound =
  | { type: "openLinkResult"; url: string; opened: boolean; reason?: string }
  | { type: "launchResult"; id: string; ok: boolean; sessionUrl?: string; reason?: string }
  | { type: "geolocationResult"; id: string; ok: boolean; position?: GeoPosition; code?: number; reason?: string };

// Per-message aliases for the Outbound members the client resolves with, derived from the
// union so the shapes can never drift from it.
export type OpenLinkResult = Extract<Outbound, { type: "openLinkResult" }>;
export type LaunchResult = Extract<Outbound, { type: "launchResult" }>;
export type GeolocationResult = Extract<Outbound, { type: "geolocationResult" }>;

// Mirror of the browser Geolocation API's `PositionOptions` (getCurrentPosition's option
// bag). Named explicitly, not aliased to the DOM type, so the wire contract is
// self-describing across the postMessage boundary.
export interface GeolocationOptions {
  enableHighAccuracy?: boolean;
  timeout?: number;
  maximumAge?: number;
}

// A plain, structured-cloneable copy of the browser's `GeolocationPosition` /
// `GeolocationCoordinates`: the live DOM objects aren't reliably cloneable across
// postMessage, so the shell flattens them before replying. Fields mirror the spec —
// `altitude`/`altitudeAccuracy`/`heading`/`speed` are `null` when the device can't
// supply them.
export interface GeoPosition {
  latitude: number;
  longitude: number;
  accuracy: number;
  altitude: number | null;
  altitudeAccuracy: number | null;
  heading: number | null;
  speed: number | null;
  timestamp: number;
}
