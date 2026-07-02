import { Anchor } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { isRoutePath, type Outbound, parseInbound, vetOpenLink } from "./bridge.ts";
import { launchRoutine } from "./client.ts";
import { ConfirmDialog, type Escalation } from "./confirm_dialog.tsx";
import { toastError, toastSuccess } from "./toast.ts";

// Haku's own UI service — a separate, Authentik-gated origin running in haku-sandbox —
// embedded as a sandboxed cross-origin iframe (the whole console is now this frame). The
// console never renders Haku's UI itself; it only frames this origin (the backend CSP
// frame-src permits the embed) and runs the trusted **bridge**: the iframe may `openLink`
// or `requestLaunch` via postMessage, but only the shell decides and acts (origin-checked
// + schema-validated). `allow-same-origin`/`allow-forms` are needed for the framed app's
// own Authentik auth; **no `allow-popups`** (only the shell opens links) and **no
// `allow="fullscreen"`**. See docs/containment.md.

// `noopener`/`noreferrer` force window.open() to return null even when the tab
// opened, so open a same-origin blank tab first. The handle is the only reliable
// popup-block signal; once it exists, sever opener before navigating it.
export function openExternal(url: string): boolean {
  const parsed = new URL(url);
  const opened = window.open(parsed.protocol === "mailto:" ? url : "about:blank", "_blank");
  if (!opened) return false;
  opened.opener = null;
  if (parsed.protocol !== "mailto:") opened.location.replace(url);
  return true;
}

const POPUP_HINT = "Allow pop-ups for this site so the console can open links.";

// Restore the route the console URL carries into the frame on first mount. The console
// hash is treated strictly as a validated path, never a URL: the src is always `uiUrl`
// with only its fragment replaced, so the frame origin stays pinned. Fragments never
// reach servers and survive the in-frame Authentik 302 chain when an SSO session already
// exists; an interactive login may drop the fragment (degrades to haku-ui's home view).
export function initialFrameSrc(uiUrl: string, consoleHash: string): string {
  const path = consoleHash.replace(/^#/, "");
  if (!isRoutePath(path)) return uiUrl;
  const src = new URL(uiUrl);
  src.hash = path;
  return src.toString();
}

export function HakuUiEmbed({ uiUrl, launchAvailable }: { uiUrl: string; launchAvailable: boolean }) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  // The single escalation awaiting the operator's trusted confirm (a link to open or a run
  // to launch). One typed action, dispatched on its `kind` — see ConfirmDialog's Escalation.
  const [pending, setPending] = useState<Escalation | null>(null);
  // Computed once: later routeChanged mirroring must not rewrite `src` (that would
  // reload the frame); the iframe navigates itself, the console only reflects.
  const [frameSrc] = useState(() => initialFrameSrc(uiUrl, window.location.hash));
  const origin = new URL(uiUrl).origin;

  function reply(msg: Outbound) {
    iframeRef.current?.contentWindow?.postMessage(msg, origin);
  }

  function openAndReply(url: string) {
    const opened = openExternal(url);
    if (!opened) toastError("Pop-up blocked", POPUP_HINT);
    reply({ type: "openLinkResult", url, opened });
  }

  useEffect(() => {
    function onMessage(e: MessageEvent) {
      if (e.origin !== origin) return; // only Haku's UI origin may talk to the shell
      const msg = parseInbound(e.data);
      if (!msg) return;
      if (msg.type === "requestLaunch") {
        // Firing the capability must be an operator gesture against trusted chrome; the
        // iframe can only ask. Refuse outright if launch isn't configured this deploy.
        if (!launchAvailable) {
          reply({ type: "launchResult", id: msg.id, ok: false, reason: "Launch is not configured." });
          return;
        }
        setPending({ kind: "launch", id: msg.id, prompt: msg.prompt });
        return;
      }
      if (msg.type === "routeChanged") {
        // Mirror the iframe's route into the console's own fragment so refresh/deep-links
        // restore the view. replaceState, not pushState: the iframe's hash navigations
        // already create joint-session-history entries, so Back works via the frame.
        history.replaceState(null, "", `#${msg.path}`);
        return;
      }
      // openLink: scheme-gate + whitelist; whitelisted opens directly, off-whitelist confirms.
      const verdict = vetOpenLink(msg.url);
      if (verdict.action === "reject") {
        toastError("Link blocked", verdict.reason);
        reply({ type: "openLinkResult", url: msg.url, opened: false, reason: verdict.reason });
      } else if (verdict.action === "open") {
        openAndReply(msg.url);
      } else {
        setPending({ kind: "openLink", url: msg.url });
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [origin, launchAvailable]);

  // The operator approved against trusted-rendered chrome — now actually perform the action
  // (open the link / fire the capability) and report the outcome back over the bridge.
  function onApprove() {
    const action = pending;
    setPending(null);
    if (!action) return;
    if (action.kind === "openLink") {
      openAndReply(action.url);
      return;
    }
    void launchRoutine(action.prompt || undefined)
      .then((result) => {
        toastSuccess(
          "Haku run launched",
          <Anchor href={result.session_url} target="_blank" rel="noreferrer">
            Open session
          </Anchor>
        );
        reply({ type: "launchResult", id: action.id, ok: true, sessionUrl: result.session_url });
      })
      .catch((e: unknown) => {
        toastError("Launch failed", e);
        reply({ type: "launchResult", id: action.id, ok: false, reason: e instanceof Error ? e.message : String(e) });
      });
  }

  function onCancel() {
    const action = pending;
    setPending(null);
    if (!action) return;
    if (action.kind === "openLink") {
      reply({ type: "openLinkResult", url: action.url, opened: false, reason: "cancelled" });
    } else {
      reply({ type: "launchResult", id: action.id, ok: false, reason: "cancelled" });
    }
  }

  return (
    <>
      <iframe
        ref={iframeRef}
        src={frameSrc}
        title="Haku UI"
        sandbox="allow-scripts allow-same-origin allow-forms"
        style={{ display: "block", width: "100vw", height: "100vh", border: 0 }}
      />
      <ConfirmDialog action={pending} onApprove={onApprove} onCancel={onCancel} />
    </>
  );
}
