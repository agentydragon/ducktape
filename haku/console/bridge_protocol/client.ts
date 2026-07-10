// Client side of the console↔iframe bridge — used by Haku's UI, which runs INSIDE the
// trusted console's sandboxed cross-origin iframe. The iframe may only **request**; the
// shell decides and acts. `createBridgeClient(shellOrigin)` binds every helper to the one
// trusted shell origin it posts to and accepts replies from (the caller supplies its own —
// haku-state reads it from its constants), then returns the bridge functions.
//
// Wire shapes come from protocol.ts (the shared source of truth); the shell
// (haku/console/frontend/bridge.ts) validates/handles the matching Inbound/Outbound.

import type { GeolocationOptions, GeolocationResult, LaunchResult, OpenLinkResult } from "./protocol.ts";

export interface BridgeClient {
  openLink: (url: string) => Promise<OpenLinkResult>;
  requestLaunch: (prompt: string) => Promise<LaunchResult>;
  requestGeolocation: (options?: GeolocationOptions) => Promise<GeolocationResult>;
  watchGeolocation: (onFix: (fix: GeolocationResult) => void, options?: GeolocationOptions) => () => void;
  notifyRouteChanged: (path: string) => void;
}

export function createBridgeClient(shellOrigin: string): BridgeClient {
  // Open `url` via the shell's bridge (the iframe is sandboxed WITHOUT allow-popups, so
  // bare `<a target=_blank>`/`window.open` are blocked). Resolves with the shell's verdict.
  function openLink(url: string): Promise<OpenLinkResult> {
    return new Promise((resolve) => {
      function onMessage(e: MessageEvent) {
        if (e.origin !== shellOrigin) return; // only the shell may reply
        const m = e.data as Partial<OpenLinkResult> | null;
        if (!m || m.type !== "openLinkResult" || m.url !== url) return;
        window.removeEventListener("message", onMessage);
        resolve({ type: "openLinkResult", url, opened: m.opened ?? false, reason: m.reason });
      }
      window.addEventListener("message", onMessage);
      window.parent.postMessage({ type: "openLink", url }, shellOrigin);
    });
  }

  // Ask the shell to launch a Haku run with `prompt` (may be empty). The shell shows its own
  // trusted confirm before firing; resolves with the outcome (a session link on success, or
  // `ok:false` with a reason). Correlated by a per-request id so a stale reply can't resolve
  // the wrong call.
  function requestLaunch(prompt: string): Promise<LaunchResult> {
    const id = crypto.randomUUID();
    return new Promise((resolve) => {
      function onMessage(e: MessageEvent) {
        if (e.origin !== shellOrigin) return; // only the shell may reply
        const m = e.data as Partial<LaunchResult> | null;
        if (!m || m.type !== "launchResult" || m.id !== id) return;
        window.removeEventListener("message", onMessage);
        resolve({ type: "launchResult", id, ok: m.ok ?? false, sessionUrl: m.sessionUrl, reason: m.reason });
      }
      window.addEventListener("message", onMessage);
      window.parent.postMessage({ type: "requestLaunch", id, prompt }, shellOrigin);
    });
  }

  // Ask the shell for the operator's current location (mirrors getCurrentPosition). Gated by
  // a shell-owned standing grant: the first call may pop a consent confirm; once allowed,
  // later calls resolve without one until the operator withdraws. Resolves with a plain
  // position, or `ok:false` + a browser-shaped `code`/`reason` (a decline/withdraw is code
  // 1, PERMISSION_DENIED). Correlated by a per-request id.
  function requestGeolocation(options?: GeolocationOptions): Promise<GeolocationResult> {
    const id = crypto.randomUUID();
    return new Promise((resolve) => {
      function onMessage(e: MessageEvent) {
        if (e.origin !== shellOrigin) return; // only the shell may reply
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
      window.parent.postMessage({ type: "requestGeolocation", id, options }, shellOrigin);
    });
  }

  // Continuously track the operator's location (mirrors watchPosition). The shell holds the
  // live watch (same standing grant as requestGeolocation) and streams each fix to `onFix`
  // tagged with this watch's id, until you call the returned stop() — or the operator
  // withdraws, which delivers a terminal `ok:false` fix (reason "withdrawn"). A per-watch id
  // keeps one watch's fixes from crossing into another's.
  function watchGeolocation(onFix: (fix: GeolocationResult) => void, options?: GeolocationOptions): () => void {
    const id = crypto.randomUUID();
    function onMessage(e: MessageEvent) {
      if (e.origin !== shellOrigin) return; // only the shell may reply
      const m = e.data as Partial<GeolocationResult> | null;
      if (!m || m.type !== "geolocationResult" || m.id !== id) return;
      onFix({ type: "geolocationResult", id, ok: m.ok ?? false, position: m.position, code: m.code, reason: m.reason });
    }
    window.addEventListener("message", onMessage);
    window.parent.postMessage({ type: "startGeolocationWatch", id, options }, shellOrigin);
    return () => {
      window.removeEventListener("message", onMessage);
      window.parent.postMessage({ type: "stopGeolocationWatch", id }, shellOrigin);
    };
  }

  // Fire-and-forget: mirror the current hash route into the shell's own URL fragment so
  // F5 / deep-links of the console restore this view. `path` is the hash minus the "#"
  // (always "/"-prefixed); the shell validates it as a strict path before replaceState.
  // Unframed (top-level) the targetOrigin check drops the self-post.
  function notifyRouteChanged(path: string): void {
    window.parent.postMessage({ type: "routeChanged", path }, shellOrigin);
  }

  return { openLink, requestLaunch, requestGeolocation, watchGeolocation, notifyRouteChanged };
}
