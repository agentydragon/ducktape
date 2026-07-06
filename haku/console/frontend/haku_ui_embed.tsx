import { ActionIcon, Anchor, Indicator } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { type GeolocationOptions, isRoutePath, type Outbound, parseInbound, vetOpenLink } from "./bridge.ts";
import { approveToolCall, denyToolCall, fetchPendingApprovals, launchRoutine, type PendingApproval } from "./client.ts";
import { ConfirmDialog, type Escalation } from "./confirm_dialog.tsx";
import { ConsolePanel, PANEL_Z } from "./console_panel.tsx";
import { GEO_PERMISSION_DENIED, GeolocationWatcher, getGeolocation } from "./geolocation.ts";
import { hasGeolocationGrant, setGeolocationGrant } from "./geolocation_grant.ts";
import { toastError, toastSuccess } from "./toast.ts";

// Haku's own UI service — a separate, Authentik-gated origin running in haku-sandbox —
// embedded as a sandboxed cross-origin iframe (the whole console is now this frame). The
// console never renders Haku's UI itself; it only frames this origin (the backend CSP
// frame-src permits the embed) and runs the trusted **bridge**: the iframe may `openLink`,
// `requestLaunch`, or read location (`requestGeolocation` one-shot / `startGeolocationWatch`
// stream) via postMessage, but only the shell decides and acts (origin-checked +
// schema-validated). `allow-same-origin`/`allow-forms` are needed for the framed app's own
// Authentik auth; **no `allow-popups`** (only the shell opens links), **no
// `allow="fullscreen"`**, and **no `allow="geolocation"`** (only the shell reads location,
// per its own consent grant; it holds every location watch). See docs/containment.md.

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
const TOOL_APPROVAL_CHANNEL = "haku-console-tool-approvals";

type ToolApprovalChannelMessage = { type: "toolApprovalsChanged" };

function isToolApprovalChannelMessage(value: unknown): value is ToolApprovalChannelMessage {
  return Boolean(
    value &&
    typeof value === "object" &&
    "type" in value &&
    (value as { type?: unknown }).type === "toolApprovalsChanged"
  );
}

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
  const [toolApprovals, setToolApprovals] = useState<PendingApproval[]>([]);
  // Computed once: later routeChanged mirroring must not rewrite `src` (that would
  // reload the frame); the iframe navigates itself, the console only reflects.
  const [frameSrc] = useState(() => initialFrameSrc(uiUrl, window.location.hash));
  const origin = new URL(uiUrl).origin;
  const toolApprovalChannelRef = useRef<BroadcastChannel | null>(null);
  const activeAction: Escalation | null =
    pending ?? (toolApprovals[0] ? { kind: "toolApproval", approval: toolApprovals[0] } : null);

  function reply(msg: Outbound) {
    iframeRef.current?.contentWindow?.postMessage(msg, origin);
  }

  function openAndReply(url: string) {
    const opened = openExternal(url);
    if (!opened) toastError("Pop-up blocked", POPUP_HINT);
    reply({ type: "openLinkResult", url, opened });
  }

  // Read the shell origin's location and report it (or a browser-shaped error) over the
  // bridge. Called on the granted fast-path AND after the operator approves the confirm.
  function geolocateAndReply(id: string, options?: GeolocationOptions) {
    void getGeolocation(options).then((r) =>
      reply(
        r.ok
          ? { type: "geolocationResult", id, ok: true, position: r.position }
          : { type: "geolocationResult", id, ok: false, code: r.code, reason: r.message }
      )
    );
  }

  // Standing consent to share location + whether a live watch is currently streaming, both
  // mirrored for the console panel (below). The message handler reads the authoritative grant
  // flag from storage (hasGeolocationGrant) directly, so it never goes stale in the effect
  // closure; these mirrors only drive rendering.
  const [geoGranted, setGeoGranted] = useState(() => hasGeolocationGrant());
  const [tracking, setTracking] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);

  // The shell (never the iframe) holds every live watchPosition stream; each fix is relayed
  // to the iframe as a geolocationResult tagged with its watch id. Created once.
  const watcherRef = useRef<GeolocationWatcher | null>(null);
  if (!watcherRef.current) {
    watcherRef.current = new GeolocationWatcher((id, e) =>
      reply(
        e.ok
          ? { type: "geolocationResult", id, ok: true, position: e.position }
          : { type: "geolocationResult", id, ok: false, code: e.code, reason: e.message }
      )
    );
  }
  const watcher = watcherRef.current;

  function startWatch(id: string, options?: GeolocationOptions) {
    watcher.start(id, options);
    setTracking(watcher.activeCount > 0);
  }
  function stopWatch(id: string) {
    watcher.stop(id);
    setTracking(watcher.activeCount > 0);
  }

  function withdrawGeolocation() {
    // Kill switch: stop every live watch, tell the iframe each ended (terminal denial), then
    // revoke the standing grant so nothing reads location again until re-granted.
    for (const id of watcher.stopAll()) {
      reply({ type: "geolocationResult", id, ok: false, code: GEO_PERMISSION_DENIED, reason: "withdrawn" });
    }
    setTracking(false);
    setGeolocationGrant(false);
    setGeoGranted(false);
  }

  function removeToolApproval(toolCallId: string) {
    setToolApprovals((approvals) => approvals.filter((a) => a.tool_call_id !== toolCallId));
  }

  function refreshToolApprovals(notifyPeers = false) {
    if (notifyPeers) toolApprovalChannelRef.current?.postMessage({ type: "toolApprovalsChanged" });
    void fetchPendingApprovals().then(
      (approvals) => setToolApprovals(approvals),
      (e: unknown) => toastError("Couldn't load tool approvals", e)
    );
  }

  // Tear down any live browser watches if the console unmounts.
  useEffect(() => () => void watcher.stopAll(), [watcher]);

  useEffect(() => {
    if (!("BroadcastChannel" in window)) return;
    const channel = new BroadcastChannel(TOOL_APPROVAL_CHANNEL);
    toolApprovalChannelRef.current = channel;
    channel.onmessage = (e: MessageEvent<unknown>) => {
      if (isToolApprovalChannelMessage(e.data)) refreshToolApprovals();
    };
    return () => {
      if (toolApprovalChannelRef.current === channel) toolApprovalChannelRef.current = null;
      channel.close();
    };
  }, []);

  useEffect(() => {
    let closed = false;
    let ws: WebSocket | null = null;
    function refreshIfLive(notifyPeers = false) {
      if (!closed) refreshToolApprovals(notifyPeers);
    }
    refreshIfLive();
    const url = new URL("/api/approvals/ws", window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(url);
    ws.onopen = () => refreshIfLive();
    ws.onmessage = () => refreshIfLive(true);
    ws.onclose = () => refreshIfLive();
    return () => {
      closed = true;
      ws?.close();
    };
  }, []);

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
      if (msg.type === "requestGeolocation") {
        // The iframe can only ask; the shell owns the standing grant. With consent already
        // given ("allow until withdrawn"), serve directly; otherwise pop the top-layer
        // consent confirm, which — once approved — records the grant so later reads skip it.
        if (hasGeolocationGrant()) geolocateAndReply(msg.id, msg.options);
        else setPending({ kind: "geolocation", id: msg.id, options: msg.options });
        return;
      }
      if (msg.type === "startGeolocationWatch") {
        // A continuous stream: same grant gate as a one-shot read, but the shell keeps the
        // watch (so the iframe can't start one silently or keep one the operator stopped).
        if (hasGeolocationGrant()) startWatch(msg.id, msg.options);
        else setPending({ kind: "geolocationWatch", id: msg.id, options: msg.options });
        return;
      }
      if (msg.type === "stopGeolocationWatch") {
        stopWatch(msg.id);
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
    const action = activeAction;
    if (pending) setPending(null);
    if (!action) return;
    if (action.kind === "openLink") {
      openAndReply(action.url);
      return;
    }
    if (action.kind === "geolocation" || action.kind === "geolocationWatch") {
      // Consent given on trusted chrome — record the standing grant so subsequent reads are
      // served without re-confirming, until the operator withdraws (the console panel below).
      setGeolocationGrant(true);
      setGeoGranted(true);
      if (action.kind === "geolocationWatch") startWatch(action.id, action.options);
      else geolocateAndReply(action.id, action.options);
      return;
    }
    if (action.kind === "toolApproval") {
      const toolCallId = action.approval.tool_call_id;
      removeToolApproval(toolCallId);
      void approveToolCall(toolCallId)
        .then((record) => {
          toastSuccess("Tool call finished", `${record.server_title}.${record.tool_name}: ${record.status}`);
          refreshToolApprovals(true);
        })
        .catch((e: unknown) => {
          toastError("Tool call failed", e);
          refreshToolApprovals(true);
        });
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
    const action = activeAction;
    if (pending) setPending(null);
    if (!action) return;
    if (action.kind === "openLink") {
      reply({ type: "openLinkResult", url: action.url, opened: false, reason: "cancelled" });
    } else if (action.kind === "geolocation" || action.kind === "geolocationWatch") {
      // Declined on trusted chrome → mirror a browser PERMISSION_DENIED so the iframe treats
      // it exactly like a native geolocation denial (for a watch, no stream ever starts). No
      // grant is recorded.
      reply({ type: "geolocationResult", id: action.id, ok: false, code: GEO_PERMISSION_DENIED, reason: "declined" });
    } else if (action.kind === "toolApproval") {
      const toolCallId = action.approval.tool_call_id;
      removeToolApproval(toolCallId);
      void denyToolCall(toolCallId, "cancelled from console").then(
        () => {
          toastSuccess("Tool call denied", action.approval.title);
          refreshToolApprovals(true);
        },
        (e: unknown) => {
          toastError("Couldn't deny tool call", e);
          refreshToolApprovals(true);
        }
      );
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
      {/* Persistent escape hatch into the shell's own controls (config, grants, views),
          rendered by the SHELL above the full-page iframe. The dot marks a standing location
          grant, and pulses (green) while a live watch is streaming, so the operator has
          at-a-glance awareness that tracking is on even before opening the panel. It only ever
          opens trusted shell chrome / *reduces* privilege, so — unlike the consent moment — it
          needn't be a top-layer surface; the browser's own site-settings revoke is the
          tamper-proof backstop (docs/containment.md). */}
      <Indicator
        color={tracking ? "teal" : "blue"}
        processing={tracking}
        disabled={!geoGranted}
        style={{ position: "fixed", top: 8, right: 8, zIndex: PANEL_Z - 1 }}
      >
        <ActionIcon variant="default" size="lg" aria-label="Open console controls" onClick={() => setPanelOpen(true)}>
          ⚙
        </ActionIcon>
      </Indicator>
      <ConsolePanel
        opened={panelOpen}
        onClose={() => setPanelOpen(false)}
        geoGranted={geoGranted}
        tracking={tracking}
        onWithdrawGeolocation={withdrawGeolocation}
      />
      <ConfirmDialog action={activeAction} onApprove={onApprove} onCancel={onCancel} />
    </>
  );
}
