// Wire contract for the console↔iframe postMessage bridge — the single source of truth
// shared by both sides:
//   - the trusted shell (ducktape haku/console: parses Inbound, sends Outbound), and
//   - Haku's UI iframe (haku-state: sends Inbound, listens for Outbound) via client.ts.
//
// Every message is a named interface discriminated by `type`; Inbound/Outbound union over
// them. Only the MESSAGE SHAPES live here — shell-only policy (the open-link whitelist, the
// inbound validators, the route-path check) deliberately stays in the shell
// (haku/console/frontend/bridge.ts), PR-gated, so a compromised iframe can't widen it.

// ── Inbound (iframe → shell): the iframe may only ask. ──────────────────────────────────

// Open an external link (the iframe is sandboxed without allow-popups).
export interface OpenLinkRequest {
  type: "openLink";
  url: string;
}

// Start a Haku run with `prompt`; the shell renders its OWN confirm before firing the
// privileged capability. `id` correlates the `LaunchResult`.
export interface LaunchRequest {
  type: "requestLaunch";
  id: string;
  prompt: string;
}

// One-shot position read (mirrors `getCurrentPosition`). The iframe has no
// `allow="geolocation"`; the shell reads its own trusted origin's location behind a standing
// operator grant. `id` correlates the `GeolocationResult`.
export interface GeolocationRequest {
  type: "requestGeolocation";
  id: string;
  options?: GeolocationOptions;
}

// Start a continuous location stream (mirrors `watchPosition`); the shell holds the live watch
// and streams each fix as a `GeolocationResult` tagged with the same `id`.
export interface GeolocationWatchStart {
  type: "startGeolocationWatch";
  id: string;
  options?: GeolocationOptions;
}

// End the watch started under the matching `id` (mirrors `clearWatch`).
export interface GeolocationWatchStop {
  type: "stopGeolocationWatch";
  id: string;
}

// Mirror the iframe's hash route into the console's URL fragment. Strictly a validated path
// (shell-side `isRoutePath`), never a URL.
export interface RouteChanged {
  type: "routeChanged";
  path: string;
}

// Capture a screenshot of the frame's own on-screen rect (mirrors a real tab/window capture,
// not a DOM serialization). The iframe cannot call `getDisplayMedia` itself (no
// `allow="display-capture"`); the shell holds a live tab-capture stream behind a standing
// operator grant and crops each request to the iframe's current `getBoundingClientRect()`.
// `id` correlates the `ScreenshotResult`.
export interface ScreenshotRequest {
  type: "requestScreenshot";
  id: string;
}

export type Inbound =
  | OpenLinkRequest
  | LaunchRequest
  | GeolocationRequest
  | GeolocationWatchStart
  | GeolocationWatchStop
  | RouteChanged
  | ScreenshotRequest;

// ── Outbound (shell → iframe): the shell's reply, so Haku's UI can react to the outcome. ──

// The shell's verdict for an `OpenLinkRequest`.
export interface OpenLinkResult {
  type: "openLinkResult";
  url: string;
  opened: boolean;
  reason?: string;
}

// The outcome of a `LaunchRequest`: a session link on success, else `ok:false` + a reason.
export interface LaunchResult {
  type: "launchResult";
  id: string;
  ok: boolean;
  sessionUrl?: string;
  reason?: string;
}

// A location fix. Answers both a one-shot `GeolocationRequest` (once) and a
// `GeolocationWatchStart` (repeatedly, same `id`, until the watch ends); the iframe correlates
// by `id`. On failure, `ok:false` with a browser-shaped `code`/`reason`.
export interface GeolocationResult {
  type: "geolocationResult";
  id: string;
  ok: boolean;
  position?: GeoPosition;
  code?: number;
  reason?: string;
}

// The captured image (a PNG data URL, already cropped to the iframe's rect) for a
// `ScreenshotRequest`, or `ok:false` + a reason (declined, withdrawn, or the browser's own
// tab-share picker was dismissed).
export interface ScreenshotResult {
  type: "screenshotResult";
  id: string;
  ok: boolean;
  imageDataUrl?: string;
  reason?: string;
}

export type Outbound = OpenLinkResult | LaunchResult | GeolocationResult | ScreenshotResult;

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
