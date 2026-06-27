import { SHELL_ORIGIN } from "./constants.ts";

// This UI runs INSIDE the trusted console's sandboxed cross-origin iframe. The
// iframe is sandboxed WITHOUT `allow-popups`, so it can't open links itself: bare
// `<a target=_blank>` / `window.open` are blocked. Instead it asks the parent shell
// to open a link by posting `{type:"openLink", url}`. The shell scheme-gates the URL
// (https/mailto only), opens whitelisted hosts directly, confirms off-whitelist hosts,
// and rejects everything else — then replies with `{type:"openLinkResult", ...}`.
// Protocol owner + whitelist live in the shell (ducktape, PR-gated), never here.
// See haku/console/frontend/bridge.ts (the shell side) and the demo in
// haku/state_template/k8s/haku-ui/index.html.

interface OpenLinkResult {
  type: "openLinkResult";
  url: string;
  opened: boolean;
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
