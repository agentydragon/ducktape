import { Anchor } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { type Outbound, parseInbound, vetOpenLink } from "./bridge.ts";
import { launchRoutine } from "./client.ts";
import { ConfirmDialog } from "./confirm_dialog.tsx";
import { toastError, toastSuccess } from "./toast.ts";

// Haku's own UI service — a separate, Authentik-gated origin running in haku-sandbox —
// embedded as a sandboxed cross-origin iframe (the whole console is now this frame). The
// console never renders Haku's UI itself; it only frames this origin (the backend CSP
// frame-src permits the embed) and runs the trusted **bridge**: the iframe may `openLink`
// or `requestLaunch` via postMessage, but only the shell decides and acts (origin-checked
// + schema-validated). `allow-same-origin`/`allow-forms` are needed for the framed app's
// own Authentik auth; **no `allow-popups`** (only the shell opens links) and **no
// `allow="fullscreen"`**. See plans/free_form_ui_iframe.md.

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

interface PendingLaunch {
  id: string;
  prompt: string;
}

export function HakuUiEmbed({ uiUrl, launchAvailable }: { uiUrl: string; launchAvailable: boolean }) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [pendingUrl, setPendingUrl] = useState<string | null>(null);
  const [pendingLaunch, setPendingLaunch] = useState<PendingLaunch | null>(null);
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
        setPendingLaunch({ id: msg.id, prompt: msg.prompt });
        return;
      }
      const verdict = vetOpenLink(msg.url);
      if (verdict.action === "reject") {
        toastError("Link blocked", verdict.reason);
        reply({ type: "openLinkResult", url: msg.url, opened: false, reason: verdict.reason });
      } else if (verdict.action === "open") {
        openAndReply(msg.url);
      } else {
        setPendingUrl(msg.url); // off-whitelist → operator confirm
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [origin, launchAvailable]);

  function onApprove() {
    const url = pendingUrl;
    setPendingUrl(null);
    if (url) openAndReply(url);
  }

  function onCancel() {
    const url = pendingUrl;
    setPendingUrl(null);
    if (url) reply({ type: "openLinkResult", url, opened: false, reason: "cancelled" });
  }

  // Launch confirm: the operator approved against trusted-rendered chrome (the prompt is
  // shown verbatim), so now actually fire the capability and report the outcome both as a
  // toast and back over the bridge so the iframe's dialog can resolve.
  function onLaunchApprove() {
    const req = pendingLaunch;
    setPendingLaunch(null);
    if (!req) return;
    void launchRoutine(req.prompt || undefined)
      .then((result) => {
        toastSuccess(
          "Haku run launched",
          <Anchor href={result.session_url} target="_blank" rel="noreferrer">
            Open session
          </Anchor>
        );
        reply({ type: "launchResult", id: req.id, ok: true, sessionUrl: result.session_url });
      })
      .catch((e: unknown) => {
        const reason = e instanceof Error ? e.message : String(e);
        toastError("Launch failed", e);
        reply({ type: "launchResult", id: req.id, ok: false, reason });
      });
  }

  function onLaunchCancel() {
    const req = pendingLaunch;
    setPendingLaunch(null);
    if (req) reply({ type: "launchResult", id: req.id, ok: false, reason: "cancelled" });
  }

  return (
    <>
      <iframe
        ref={iframeRef}
        src={uiUrl}
        title="Haku UI"
        sandbox="allow-scripts allow-same-origin allow-forms"
        style={{ display: "block", width: "100vw", height: "100vh", border: 0 }}
      />
      <ConfirmDialog
        request={pendingUrl ? { title: "Open this link?", url: pendingUrl, approveLabel: "Open" } : null}
        onApprove={onApprove}
        onCancel={onCancel}
      />
      <ConfirmDialog
        request={
          pendingLaunch
            ? {
                title: "Launch a Haku run?",
                body: "This starts a new Claude Code web session running Haku now.",
                preview: pendingLaunch.prompt || undefined,
                approveLabel: "Launch",
              }
            : null
        }
        onApprove={onLaunchApprove}
        onCancel={onLaunchCancel}
      />
    </>
  );
}
