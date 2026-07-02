// postMessage protocol between the trusted shell (this console) and Haku's
// agent-authored UI iframe. The iframe may only **request**; the shell decides and
// acts. Every inbound message is origin-checked and schema-validated.
// See docs/containment.md → "The bridge protocol".
//
// AUTHORITATIVE COPY of the iframe protocol contract. Haku's UI (the client side)
// keeps a hand-maintained DUPLICATE of the message shapes in `haku-state` (seeded
// from `haku/state_template/ui/`); this file is the source of truth — keep the two in
// sync. See haku/PLAN.md → _Not yet built_ (share-the-protocol TODO).

// Inbound (iframe → shell). The iframe may only **ask**:
//  - `openLink`: open an external link (the iframe is sandboxed without allow-popups).
//  - `requestLaunch`: start a Haku run with `prompt`. Firing the privileged launch
//    capability must be a genuine operator gesture against trusted chrome, so the iframe
//    can only request it; the shell renders its OWN confirm (showing the prompt) and only
//    then fires. `id` correlates the eventual `launchResult`.
export type Inbound = { type: "openLink"; url: string } | { type: "requestLaunch"; id: string; prompt: string };

// Outbound result (shell → iframe), so Haku's UI can react to the outcome.
export type Outbound =
  | { type: "openLinkResult"; url: string; opened: boolean; reason?: string }
  | { type: "launchResult"; id: string; ok: boolean; sessionUrl?: string; reason?: string };

// Narrow an untrusted postMessage payload to a known message, or null.
export function parseInbound(data: unknown): Inbound | null {
  if (!data || typeof data !== "object") return null;
  const m = data as Record<string, unknown>;
  if (m.type === "openLink" && typeof m.url === "string") return { type: "openLink", url: m.url };
  if (m.type === "requestLaunch" && typeof m.id === "string" && typeof m.prompt === "string") {
    return { type: "requestLaunch", id: m.id, prompt: m.prompt };
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
