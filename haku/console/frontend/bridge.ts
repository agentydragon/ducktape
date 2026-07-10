// Shell side of the console↔iframe postMessage protocol: the trusted shell (this console)
// origin-checks and schema-validates every inbound request from Haku's agent-authored UI
// iframe, then decides and acts. The iframe may only **request**.
// See docs/containment.md → "The bridge protocol".
//
// The wire shapes (Inbound/Outbound/GeolocationOptions/GeoPosition) plus the client helpers
// live in the shared @haku/console-bridge package (haku/console/bridge_protocol) — the one
// source of truth both sides import. What stays HERE is shell-only and deliberately NOT
// shared: the inbound validators and the open-link whitelist, PR-gated so a compromised
// iframe can't widen them.
import type { GeolocationOptions, Inbound } from "@haku/console-bridge/protocol";

// A mirrored route is strictly a PATH, never a URL: leading `/` (but not a
// protocol-relative `//`, so the value stays inert even if a future caller drops it into
// a URL context), a length cap, and a conservative charset — `/` plus what haku-ui's
// per-segment encodeURIComponent can emit, so `%` only as a well-formed `%XX` escape
// (malformed escapes get normalized inconsistently by URL serializers) — so a hostile
// iframe can't put arbitrary content in the console's URL bar.
export const ROUTE_PATH_MAX_LENGTH = 512;
const ROUTE_PATH_RE = /^\/(?:[A-Za-z0-9/._~!'()*-]|%[0-9A-Fa-f]{2})*$/;
export function isRoutePath(path: string): boolean {
  return path.length <= ROUTE_PATH_MAX_LENGTH && !path.startsWith("//") && ROUTE_PATH_RE.test(path);
}

// Pick only the recognized option fields with their correct types, dropping anything
// unknown or mistyped — the browser's getCurrentPosition is itself lenient about its
// option bag, and we never want a malformed `options` to reject the whole request.
export function parseGeolocationOptions(raw: unknown): GeolocationOptions | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const o = raw as Record<string, unknown>;
  const opts: GeolocationOptions = {};
  if (typeof o.enableHighAccuracy === "boolean") opts.enableHighAccuracy = o.enableHighAccuracy;
  if (typeof o.timeout === "number") opts.timeout = o.timeout;
  if (typeof o.maximumAge === "number") opts.maximumAge = o.maximumAge;
  return opts;
}

// Narrow an untrusted postMessage payload to a known message, or null.
export function parseInbound(data: unknown): Inbound | null {
  if (!data || typeof data !== "object") return null;
  const m = data as Record<string, unknown>;
  if (m.type === "openLink" && typeof m.url === "string") return { type: "openLink", url: m.url };
  if (m.type === "requestLaunch" && typeof m.id === "string" && typeof m.prompt === "string") {
    return { type: "requestLaunch", id: m.id, prompt: m.prompt };
  }
  if (m.type === "requestGeolocation" && typeof m.id === "string") {
    return { type: "requestGeolocation", id: m.id, options: parseGeolocationOptions(m.options) };
  }
  if (m.type === "startGeolocationWatch" && typeof m.id === "string") {
    return { type: "startGeolocationWatch", id: m.id, options: parseGeolocationOptions(m.options) };
  }
  if (m.type === "stopGeolocationWatch" && typeof m.id === "string") {
    return { type: "stopGeolocationWatch", id: m.id };
  }
  if (m.type === "routeChanged" && typeof m.path === "string" && isRoutePath(m.path)) {
    return { type: "routeChanged", path: m.path };
  }
  return null;
}

// Operator-owned trusted whitelist — it lives in the **shell** (ducktape, PR-gated),
// deliberately NOT in haku-state, or Haku could whitelist a phishing host to skip the
// confirm. An entry matches the host exactly or any subdomain of it.
export const OPEN_LINK_WHITELIST = [
  "claude.ai",
  "github.com",
  "allegedly.works", // the operator's own services (*.allegedly.works)
  "mail.google.com",
  "drive.google.com",
  "calendar.google.com",
  "app.tana.inc",
];

export type LinkVerdict = { action: "open" } | { action: "confirm" } | { action: "reject"; reason: string };

// Scheme is a HARD gate (never behind a confirm): only `https` + `mailto` — opening
// `javascript:`/`data:`/`blob:`/`file:` in the top context would run code in the
// shell's origin. The host whitelist only decides warn-vs-not for `https`.
export function vetOpenLink(rawUrl: string): LinkVerdict {
  let u: URL;
  try {
    u = new URL(rawUrl);
  } catch {
    return { action: "reject", reason: "unparseable URL" };
  }
  if (u.protocol === "mailto:") return { action: "open" };
  if (u.protocol !== "https:") {
    return { action: "reject", reason: `scheme "${u.protocol}" not allowed (https/mailto only)` };
  }
  const host = u.hostname.toLowerCase();
  const ok = OPEN_LINK_WHITELIST.some((w) => host === w || host.endsWith(`.${w}`));
  return { action: ok ? "open" : "confirm" };
}
