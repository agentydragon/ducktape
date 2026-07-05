import { SHELL_ORIGIN } from "./constants.ts";

// This UI runs INSIDE the trusted console's sandboxed cross-origin iframe. The iframe
// may only **request**; the shell (ducktape, PR-gated) decides and acts. Three requests
// (plus the fire-and-forget `routeChanged` notify at the bottom of this file):
//
//  - `openLink`: the iframe is sandboxed WITHOUT `allow-popups`, so it can't open links
//    itself (bare `<a target=_blank>` / `window.open` are blocked). It posts
//    `{type:"openLink", url}`; the shell scheme-gates (https/mailto), opens whitelisted
//    hosts directly, confirms off-whitelist, rejects the rest → `{type:"openLinkResult"}`.
//  - `requestLaunch`: the iframe asks the shell to start a Haku run with a prompt. Firing
//    the privileged launch capability must be a genuine operator gesture against
//    trusted-rendered chrome, so the iframe can only *ask* — the shell shows its OWN
//    confirm and only then fires → `{type:"launchResult"}`. The iframe can render the
//    prompt dialog; it can never script the launch.
//  - `requestGeolocation`: the iframe asks the shell for the operator's location, mirroring
//    the browser Geolocation API. The iframe has NO `allow="geolocation"`, so it can't read
//    location itself; the shell reads its own (trusted) origin's location, gated by a
//    shell-owned standing grant ("allow until withdrawn") — first ask pops a consent
//    confirm; once allowed, later asks are served → `{type:"geolocationResult"}`.
//
// DUPLICATE: the result shapes + message `type` strings below are a hand-maintained copy
// of the AUTHORITATIVE protocol in ducktape's haku/console/frontend/bridge.ts (the shell
// side). Keep the two in sync by hand. TODO: share one protocol definition instead of
// duplicating it (see haku/console/plans/free_form_ui_iframe.md → Open questions).

interface OpenLinkResult {
  type: "openLinkResult";
  url: string;
  opened: boolean;
  reason?: string;
}

export interface LaunchResult {
  type: "launchResult";
  id: string;
  ok: boolean;
  sessionUrl?: string;
  reason?: string;
}

// Mirror of the browser Geolocation API's option bag / position. Kept in sync with the
// shell's GeolocationOptions + GeoPosition (ducktape haku/console/frontend/bridge.ts).
export interface GeolocationOptions {
  enableHighAccuracy?: boolean;
  timeout?: number;
  maximumAge?: number;
}

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

export interface GeolocationResult {
  type: "geolocationResult";
  id: string;
  ok: boolean;
  position?: GeoPosition;
  // On failure, the browser GeolocationPositionError.code (1 PERMISSION_DENIED — also used
  // when the operator declines or withdraws — 2 POSITION_UNAVAILABLE, 3 TIMEOUT) + message.
  code?: number;
  reason?: string;
}

// Open `url` via the shell's bridge. Resolves with the shell's verdict.
export function openLink(url: string): Promise<OpenLinkResult> {
  return new Promise((resolve) => {
    function onMessage(e: MessageEvent) {
      if (e.origin !== SHELL_ORIGIN) return; // only the shell may reply
      const m = e.data as Partial<OpenLinkResult> | null;
      if (!m || m.type !== "openLinkResult" || m.url !== url) return;
      window.removeEventListener("message", onMessage);
      resolve({ type: "openLinkResult", url, opened: m.opened ?? false, reason: m.reason });
    }
    window.addEventListener("message", onMessage);
    window.parent.postMessage({ type: "openLink", url }, SHELL_ORIGIN);
  });
}

// Ask the shell to launch a Haku run with `prompt` (may be empty). The shell shows its
// own trusted confirm before firing; resolves with the outcome (a session link on
// success, or `ok:false` with a reason — e.g. the operator cancelled). Correlated by a
// per-request id so a stale reply can't resolve the wrong call.
export function requestLaunch(prompt: string): Promise<LaunchResult> {
  const id = crypto.randomUUID();
  return new Promise((resolve) => {
    function onMessage(e: MessageEvent) {
      if (e.origin !== SHELL_ORIGIN) return; // only the shell may reply
      const m = e.data as Partial<LaunchResult> | null;
      if (!m || m.type !== "launchResult" || m.id !== id) return;
      window.removeEventListener("message", onMessage);
      resolve({ type: "launchResult", id, ok: m.ok ?? false, sessionUrl: m.sessionUrl, reason: m.reason });
    }
    window.addEventListener("message", onMessage);
    window.parent.postMessage({ type: "requestLaunch", id, prompt }, SHELL_ORIGIN);
  });
}

// Ask the shell for the operator's current location, mirroring the browser Geolocation
// API's getCurrentPosition. The shell gates it behind a standing operator grant ("allow
// until withdrawn"): the first call may pop the shell's consent confirm; once allowed,
// later calls resolve without one, until the operator withdraws in the console panel.
// Resolves with a plain position on success, or `ok:false` + a browser-shaped
// `code`/`reason` (a decline/withdraw is code 1, PERMISSION_DENIED). Correlated by a
// per-request id so a stale reply can't resolve the wrong call.
export function requestGeolocation(options?: GeolocationOptions): Promise<GeolocationResult> {
  const id = crypto.randomUUID();
  return new Promise((resolve) => {
    function onMessage(e: MessageEvent) {
      if (e.origin !== SHELL_ORIGIN) return; // only the shell may reply
      const m = e.data as Partial<GeolocationResult> | null;
      if (!m || m.type !== "geolocationResult" || m.id !== id) return;
      window.removeEventListener("message", onMessage);
      resolve({
        type: "geolocationResult",
        id,
        ok: m.ok ?? false,
        position: m.position,
        code: m.code,
        reason: m.reason,
      });
    }
    window.addEventListener("message", onMessage);
    window.parent.postMessage({ type: "requestGeolocation", id, options }, SHELL_ORIGIN);
  });
}

// Fire-and-forget: mirror the current hash route into the console shell's own URL
// fragment so F5 / deep-links of the console restore this view (shell contract:
// ducktape haku/console/docs/containment.md → `routeChanged`). `path` is the hash minus
// the "#" (always "/"-prefixed); the shell validates it as a strict path before
// replaceState. Unframed (top-level) the targetOrigin check drops the self-post.
export function notifyRouteChanged(path: string): void {
  window.parent.postMessage({ type: "routeChanged", path }, SHELL_ORIGIN);
}
