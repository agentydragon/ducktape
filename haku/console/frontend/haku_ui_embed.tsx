import { useEffect, useRef, useState } from "react";

import { type Outbound, parseInbound, vetOpenLink } from "./bridge.ts";
import { ConfirmDialog } from "./confirm_dialog.tsx";
import { toastError } from "./toast.ts";

// Haku's own UI service — a separate, Authentik-gated origin running in haku-sandbox —
// embedded as a sandboxed cross-origin iframe (the "Free-form UI" tab). The console
// never renders Haku's UI itself; it only frames this origin (the backend CSP frame-src
// permits the embed) and runs the trusted **bridge**: the iframe may `openLink` via
// postMessage, but only the shell decides and acts (origin-checked + schema-validated +
// scheme-gated + whitelist/confirm). `allow-same-origin`/`allow-forms` are needed for
// the framed app's own Authentik auth; **no `allow-popups`** (only the shell opens
// links) and **no `allow="fullscreen"`**. See plans/free_form_ui_iframe.md.

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

export function HakuUiEmbed({ uiUrl }: { uiUrl: string }) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [pendingUrl, setPendingUrl] = useState<string | null>(null);
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
  }, [origin]);

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

  return (
    <>
      <iframe
        ref={iframeRef}
        src={uiUrl}
        title="Haku UI"
        sandbox="allow-scripts allow-same-origin allow-forms"
        className="mt-4 w-full"
        style={{ height: "80vh", border: 0 }}
      />
      <ConfirmDialog
        request={pendingUrl ? { title: "Open this link?", url: pendingUrl, approveLabel: "Open" } : null}
        onApprove={onApprove}
        onCancel={onCancel}
      />
    </>
  );
}
