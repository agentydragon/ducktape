// Client side of the console↔iframe bridge — used by Haku's UI, which runs INSIDE the
// trusted console's sandboxed cross-origin iframe. The iframe may only **request**; the
// shell decides and acts. `createBridgeClient(shellOrigin)` binds every helper to the one
// trusted shell origin it posts to and accepts replies from (the caller supplies its own —
// haku-state reads it from its constants), then returns the bridge functions.
//
// Wire shapes come from protocol.ts (the shared source of truth); the shell
// (haku/console/frontend/bridge.ts) validates/handles the matching Inbound/Outbound.

import type {
  GeolocationOptions,
  GeolocationResult,
  Inbound,
  LaunchResult,
  OpenLinkResult,
  Outbound,
  ScreenshotResult,
} from "./protocol.ts";

export interface BridgeClient {
  openLink: (url: string) => Promise<OpenLinkResult>;
  requestLaunch: (prompt: string) => Promise<LaunchResult>;
  requestGeolocation: (options?: GeolocationOptions) => Promise<GeolocationResult>;
  watchGeolocation: (onFix: (fix: GeolocationResult) => void, options?: GeolocationOptions) => () => void;
  notifyRouteChanged: (path: string) => void;
  requestScreenshot: () => Promise<ScreenshotResult>;
}

export function createBridgeClient(shellOrigin: string): BridgeClient {
  // Every verb below is the same shape — post one request to the trusted shell, then correlate
  // its replies — so the post, the origin check, and the listener lifecycle live here once
  // instead of being re-implemented per verb.
  const post = (message: Inbound): void => window.parent.postMessage(message, shellOrigin);

  // Post `request`, then hand every shell reply that `match` accepts to `onReply` until the
  // returned stop() removes the listener. Only messages from the trusted shell origin count.
  function subscribe<T extends Outbound>(
    request: Inbound,
    match: (m: Outbound) => m is T,
    onReply: (m: T) => void
  ): () => void {
    function onMessage(e: MessageEvent): void {
      if (e.origin !== shellOrigin) return; // only the shell may reply
      const m = e.data as Outbound | null;
      if (m && match(m)) onReply(m);
    }
    window.addEventListener("message", onMessage);
    post(request);
    return () => window.removeEventListener("message", onMessage);
  }

  // One request → the first correlated reply, then unsubscribe.
  function requestReply<T extends Outbound>(request: Inbound, match: (m: Outbound) => m is T): Promise<T> {
    return new Promise((resolve) => {
      const stop = subscribe(request, match, (m) => {
        stop();
        resolve(m);
      });
    });
  }

  return {
    // Open `url` via the shell (the iframe is sandboxed WITHOUT allow-popups, so bare
    // `<a target=_blank>`/`window.open` are blocked). Correlated by url.
    openLink: (url) =>
      requestReply({ type: "openLink", url }, (m): m is OpenLinkResult => m.type === "openLinkResult" && m.url === url),

    // Ask the shell to launch a Haku run with `prompt` (may be empty); the shell shows its own
    // trusted confirm before firing. Correlated by a per-request id so a stale reply can't
    // resolve the wrong call.
    requestLaunch: (prompt) => {
      const id = crypto.randomUUID();
      return requestReply(
        { type: "requestLaunch", id, prompt },
        (m): m is LaunchResult => m.type === "launchResult" && m.id === id
      );
    },

    // Ask the shell for the operator's current location (mirrors getCurrentPosition), gated by a
    // shell-owned standing grant. Resolves with a position, or `ok:false` + a browser-shaped
    // `code`/`reason` (a decline/withdraw is code 1, PERMISSION_DENIED).
    requestGeolocation: (options) => {
      const id = crypto.randomUUID();
      return requestReply(
        { type: "requestGeolocation", id, options },
        (m): m is GeolocationResult => m.type === "geolocationResult" && m.id === id
      );
    },

    // Continuously track the operator's location (mirrors watchPosition): the shell holds the
    // live watch and streams each fix to `onFix` until the returned stop() — or the operator
    // withdraws, which delivers a terminal `ok:false` fix (reason "withdrawn").
    watchGeolocation: (onFix, options) => {
      const id = crypto.randomUUID();
      const stop = subscribe(
        { type: "startGeolocationWatch", id, options },
        (m): m is GeolocationResult => m.type === "geolocationResult" && m.id === id,
        onFix
      );
      return () => {
        stop();
        post({ type: "stopGeolocationWatch", id });
      };
    },

    // Fire-and-forget: mirror the current hash route into the shell's own URL fragment so
    // F5 / deep-links of the console restore this view. Unframed, the targetOrigin check drops
    // the self-post.
    notifyRouteChanged: (path) => post({ type: "routeChanged", path }),

    // Ask the shell for a screenshot of its own on-screen rect (a real capture, cropped to the
    // iframe — not a DOM serialization), gated by a shell-owned standing grant. Resolves
    // `ok:false` + `reason` on decline/withdraw/picker-dismissed.
    requestScreenshot: () => {
      const id = crypto.randomUUID();
      return requestReply(
        { type: "requestScreenshot", id },
        (m): m is ScreenshotResult => m.type === "screenshotResult" && m.id === id
      );
    },
  };
}
