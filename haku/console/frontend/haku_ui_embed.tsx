import { Anchor } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import {
  geolocationApprovalQueueId,
  makeRecentToolCall,
  screenshotApprovalQueueId,
  toolApprovalQueueId,
  type GeolocationApproval,
  type RecentToolCall,
  type ScreenshotApproval,
} from "./approval_state.ts";
import { type GeolocationOptions, type Outbound } from "@haku/console-bridge/protocol";

import { isRoutePath, parseInbound, vetOpenLink } from "./bridge.ts";
import { fetchPendingApprovals, launchRoutine, type PendingApproval, type ToolCallRecord } from "./client.ts";
import { ConfirmDialog, type Escalation } from "./confirm_dialog.tsx";
import { ShellChrome } from "./shell_chrome.tsx";
import { GEO_PERMISSION_DENIED, GeolocationWatcher, getGeolocation } from "./geolocation.ts";
import { hasGeolocationGrant, setGeolocationGrant } from "./geolocation_grant.ts";
import { openExternal, POPUP_HINT } from "./open_external.ts";
import { rememberEmbedPath, viewForPathname } from "./routing.ts";
import { ScreenshotSession } from "./screenshot_capture.ts";
import { hasScreenshotGrant, setScreenshotGrant } from "./screenshot_grant.ts";
import { toastError, toastSuccess } from "./toast.ts";
import { useToolCallDecision } from "./tool_call_decision.ts";
import { useConsoleEvents } from "./console_events.ts";

// Haku's own UI service — a separate, Authentik-gated origin running in haku-sandbox —
// embedded as a sandboxed cross-origin iframe (the whole console is now this frame). The
// console never renders Haku's UI itself; it only frames this origin (the backend CSP
// frame-src permits the embed) and runs the trusted **bridge**: the iframe may `openLink`,
// `requestLaunch`, read location (`requestGeolocation` one-shot / `startGeolocationWatch`
// stream), or `requestScreenshot` (a real tab-capture crop, not a DOM serialization) via
// postMessage, but only the shell decides and acts (origin-checked + schema-validated).
// `allow-same-origin`/`allow-forms` are needed for the framed app's own Authentik auth; **no
// `allow-popups`** (only the shell opens links), **no `allow="fullscreen"`**, **no
// `allow="geolocation"`** (only the shell reads location, per its own consent grant; it holds
// every location watch), and **no `allow="display-capture"`** (only the shell captures screen
// content, per its own consent grant; it holds the one live capture stream). See
// docs/containment.md.

// Restore the route the console URL carries into the frame on first mount. haku-ui
// speaks real History-API paths now, so the console pathname mirrors the frame route
// directly; a legacy `#/…` console URL still wins when present (old bookmarks). The
// input is treated strictly as a validated path, never a URL: the src is always `uiUrl`
// with only its pathname replaced, so the frame origin stays pinned. Paths (unlike the
// old fragments) survive the whole in-frame Authentik redirect chain, interactive login
// included — the rd parameter carries them.
export function initialFrameSrc(uiUrl: string, routePath: string): string {
  const path = routePath.replace(/^#/, "");
  if (!isRoutePath(path)) return uiUrl;
  const src = new URL(uiUrl);
  src.pathname = path;
  return src.toString();
}

/** The haku-ui route the current console URL carries: a legacy `#/…` fragment wins
 * (old-form deep links always mean "open that"); otherwise the console pathname —
 * unless it's a console-own view's path, which carries no frame route. */
export function routeFromLocation(loc: { pathname: string; hash: string }): string {
  if (loc.hash.startsWith("#/")) return loc.hash.slice(1);
  return viewForPathname(loc.pathname) === "embed" ? loc.pathname : "/";
}

export function HakuUiEmbed({
  uiUrl,
  launchAvailable,
  onOpenToolCalls,
}: {
  uiUrl: string;
  launchAvailable: boolean;
  onOpenToolCalls: () => void;
}) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  // The single escalation awaiting the operator's trusted confirm (a link to open or a run
  // to launch). One typed action, dispatched on its `kind` — see ConfirmDialog's Escalation.
  const [pending, setPending] = useState<Escalation | null>(null);
  const [toolApprovals, setToolApprovals] = useState<PendingApproval[]>([]);
  const toolApprovalsRef = useRef<PendingApproval[]>([]);
  const knownToolApprovalIdsRef = useRef<Set<string> | null>(null);
  const [geolocationApprovals, setGeolocationApprovals] = useState<GeolocationApproval[]>([]);
  const geolocationApprovalsRef = useRef<GeolocationApproval[]>([]);
  const [screenshotApprovals, setScreenshotApprovals] = useState<ScreenshotApproval[]>([]);
  const screenshotApprovalsRef = useRef<ScreenshotApproval[]>([]);
  const [approvalsOpen, setApprovalsOpen] = useState(false);
  const [decidingNonToolApprovalIds, setDecidingNonToolApprovalIds] = useState<string[]>([]);
  const [recentToolCalls, setRecentToolCalls] = useState<RecentToolCall[]>([]);
  // Computed once: later routeChanged mirroring must not rewrite `src` (that would
  // reload the frame); the iframe navigates itself, the console only reflects.
  const [frameSrc] = useState(() => initialFrameSrc(uiUrl, routeFromLocation(window.location)));
  const origin = new URL(uiUrl).origin;
  const activeAction: Escalation | null = pending;

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
  // mirrored for the shell chrome (below). The message handler reads the authoritative grant
  // flag from storage (hasGeolocationGrant) directly, so it never goes stale in the effect
  // closure; these mirrors only drive rendering.
  const [geoGranted, setGeoGranted] = useState(() => hasGeolocationGrant());
  const [tracking, setTracking] = useState(false);

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

  // Standing consent to capture screenshots + whether the shell currently holds a live
  // tab-capture stream (mirrors geoGranted/tracking above).
  const [screenshotGranted, setScreenshotGranted] = useState(() => hasScreenshotGrant());
  const [sharingScreen, setSharingScreen] = useState(false);

  // The shell (never the iframe) holds the one live getDisplayMedia stream, reused across
  // requests so only the first capture (or the first after the operator's browser-native "Stop
  // sharing") needs the native picker. Created once.
  const screenshotSessionRef = useRef<ScreenshotSession | null>(null);
  if (!screenshotSessionRef.current) {
    screenshotSessionRef.current = new ScreenshotSession(() => setSharingScreen(false));
  }
  const screenshotSession = screenshotSessionRef.current;

  // Read the current frame and reply. Only meaningful while the session is active — the
  // approval flow below is what starts it (getDisplayMedia needs a user gesture, so it can
  // only be called from the Approve click, never from here).
  function captureAndReplyScreenshot(id: string) {
    const rect = iframeRef.current?.getBoundingClientRect();
    const imageDataUrl = rect ? screenshotSession.captureFrame(rect) : null;
    reply(
      imageDataUrl
        ? { type: "screenshotResult", id, ok: true, imageDataUrl }
        : { type: "screenshotResult", id, ok: false, reason: "capture failed" }
    );
  }

  function withdrawScreenshot() {
    screenshotSession.stop();
    setSharingScreen(false);
    setScreenshotGrant(false);
    setScreenshotGranted(false);
  }

  function openApprovalQueue() {
    setApprovalsOpen(true);
  }

  function setNonToolDeciding(id: string, deciding: boolean) {
    setDecidingNonToolApprovalIds((ids) =>
      deciding ? (ids.includes(id) ? ids : [...ids, id]) : ids.filter((existing) => existing !== id)
    );
  }

  function applyToolApprovals(approvals: PendingApproval[]) {
    const incomingIds = new Set(approvals.map((approval) => approval.tool_call_id));
    const previousIds = knownToolApprovalIdsRef.current;
    const newApprovals =
      previousIds === null ? approvals : approvals.filter((approval) => !previousIds.has(approval.tool_call_id));
    knownToolApprovalIdsRef.current = incomingIds;
    toolApprovalsRef.current = approvals;
    setToolApprovals(approvals);
    if (newApprovals.length > 0) {
      openApprovalQueue();
    }
  }

  function removeToolApproval(toolCallId: string): PendingApproval[] {
    const remaining = toolApprovalsRef.current.filter((approval) => approval.tool_call_id !== toolCallId);
    toolApprovalsRef.current = remaining;
    knownToolApprovalIdsRef.current = new Set(remaining.map((approval) => approval.tool_call_id));
    setToolApprovals(remaining);
    return remaining;
  }

  function addRecentToolCall(record: ToolCallRecord) {
    const recent = makeRecentToolCall(record, Date.now());
    if (!recent) return;
    setRecentToolCalls((records) => [
      recent,
      ...records.filter((existing) => existing.record.tool_call_id !== record.tool_call_id),
    ]);
  }

  function finishToolDecision(record: ToolCallRecord) {
    removeToolApproval(record.tool_call_id);
    addRecentToolCall(record);
  }

  function addGeolocationApproval(mode: GeolocationApproval["mode"], id: string, options?: GeolocationOptions) {
    const approval: GeolocationApproval = { id, mode, options, createdAt: new Date().toISOString() };
    const remaining = [approval, ...geolocationApprovalsRef.current.filter((existing) => existing.id !== id)];
    geolocationApprovalsRef.current = remaining;
    setGeolocationApprovals(remaining);
    openApprovalQueue();
  }

  function removeGeolocationApproval(id: string): GeolocationApproval[] {
    const remaining = geolocationApprovalsRef.current.filter((approval) => approval.id !== id);
    geolocationApprovalsRef.current = remaining;
    setGeolocationApprovals(remaining);
    return remaining;
  }

  function advanceAfterGeolocation(id: string) {
    removeGeolocationApproval(id);
  }

  function addScreenshotApproval(id: string) {
    const approval: ScreenshotApproval = { id, createdAt: new Date().toISOString() };
    const remaining = [approval, ...screenshotApprovalsRef.current.filter((existing) => existing.id !== id)];
    screenshotApprovalsRef.current = remaining;
    setScreenshotApprovals(remaining);
    openApprovalQueue();
  }

  function removeScreenshotApproval(id: string): ScreenshotApproval[] {
    const remaining = screenshotApprovalsRef.current.filter((approval) => approval.id !== id);
    screenshotApprovalsRef.current = remaining;
    setScreenshotApprovals(remaining);
    return remaining;
  }

  function advanceAfterScreenshot(id: string) {
    removeScreenshotApproval(id);
  }

  function refreshToolApprovals() {
    void fetchPendingApprovals().then(
      (approvals) => applyToolApprovals(approvals),
      (e: unknown) => toastError("Couldn't load tool approvals", e)
    );
  }

  const toolDecisions = useToolCallDecision({
    onSuccess: finishToolDecision,
    onSettled: refreshToolApprovals,
  });
  const decidingApprovalIds = [
    ...decidingNonToolApprovalIds,
    ...Array.from(toolDecisions.decidingToolCallIds, toolApprovalQueueId),
  ];

  // Live tool-call signal: initial fetch on mount plus a refetch on every server WS event
  // (which the server also broadcasts to every other tab, so they refresh too). Its status
  // drives the shell's live-channel warning when the socket is down.
  const liveStatus = useConsoleEvents(refreshToolApprovals);

  // Tear down any live browser watches / capture stream if the console unmounts.
  useEffect(() => () => void watcher.stopAll(), [watcher]);
  useEffect(() => () => screenshotSession.stop(), [screenshotSession]);

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
        // given ("allow until withdrawn"), serve directly; otherwise queue a trusted shell
        // approval in the non-modal approvals panel so Haku's UI remains usable.
        if (hasGeolocationGrant()) geolocateAndReply(msg.id, msg.options);
        else addGeolocationApproval("geolocation", msg.id, msg.options);
        return;
      }
      if (msg.type === "startGeolocationWatch") {
        // A continuous stream: same grant gate as a one-shot read, but the shell keeps the
        // watch (so the iframe can't start one silently or keep one the operator stopped).
        if (hasGeolocationGrant()) startWatch(msg.id, msg.options);
        else addGeolocationApproval("geolocationWatch", msg.id, msg.options);
        return;
      }
      if (msg.type === "stopGeolocationWatch") {
        stopWatch(msg.id);
        return;
      }
      if (msg.type === "requestScreenshot") {
        // Same grant-gate shape as geolocation, but capture ALSO needs the browser's own
        // tab-share picker (no persistent silent grant for getDisplayMedia) — so even with the
        // standing grant set, a dead session (operator stopped sharing) still needs a fresh
        // approval to re-open the picker from a real click.
        if (hasScreenshotGrant() && screenshotSession.active) captureAndReplyScreenshot(msg.id);
        else addScreenshotApproval(msg.id);
        return;
      }
      if (msg.type === "routeChanged") {
        // Mirror the iframe's route into the console's own pathname so refresh/deep-links
        // restore the view (path-form URLs are the copyable ones — operator, 2026-07-13).
        // replaceState, not pushState: the iframe's own history navigations already create
        // joint-session-history entries, so Back works via the frame. Skip while a
        // console-own view (e.g. /tool-calls) holds the pathname — just remember the
        // route for the return trip.
        rememberEmbedPath(msg.path);
        if (viewForPathname(window.location.pathname) === "embed") {
          history.replaceState(null, "", msg.path);
        }
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

  useEffect(() => {
    toolApprovalsRef.current = toolApprovals;
  }, [toolApprovals]);

  useEffect(() => {
    geolocationApprovalsRef.current = geolocationApprovals;
  }, [geolocationApprovals]);

  useEffect(() => {
    screenshotApprovalsRef.current = screenshotApprovals;
  }, [screenshotApprovals]);

  useEffect(() => {
    if (recentToolCalls.length === 0) return;
    const nextHideAtMs = Math.min(...recentToolCalls.map((recent) => recent.hideAtMs));
    const t = window.setTimeout(
      () => {
        const now = Date.now();
        setRecentToolCalls((records) => records.filter((recent) => recent.hideAtMs > now));
      },
      Math.max(0, nextHideAtMs - Date.now()) + 50
    );
    return () => window.clearTimeout(t);
  }, [recentToolCalls]);

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
    } else {
      reply({ type: "launchResult", id: action.id, ok: false, reason: "cancelled" });
    }
  }

  function approveToolApproval(approval: PendingApproval) {
    void toolDecisions.approve(approval);
  }

  function denyToolApproval(approval: PendingApproval, reason?: string) {
    void toolDecisions.deny(approval, reason);
  }

  function approveGeolocationApproval(approval: GeolocationApproval) {
    const approvalId = geolocationApprovalQueueId(approval.id);
    setNonToolDeciding(approvalId, true);
    setGeolocationGrant(true);
    setGeoGranted(true);
    if (approval.mode === "geolocationWatch") startWatch(approval.id, approval.options);
    else geolocateAndReply(approval.id, approval.options);
    setNonToolDeciding(approvalId, false);
    advanceAfterGeolocation(approval.id);
  }

  function denyGeolocationApproval(approval: GeolocationApproval) {
    const approvalId = geolocationApprovalQueueId(approval.id);
    setNonToolDeciding(approvalId, true);
    reply({ type: "geolocationResult", id: approval.id, ok: false, code: GEO_PERMISSION_DENIED, reason: "declined" });
    setNonToolDeciding(approvalId, false);
    advanceAfterGeolocation(approval.id);
  }

  function approveScreenshotApproval(approval: ScreenshotApproval) {
    const approvalId = screenshotApprovalQueueId(approval.id);
    setNonToolDeciding(approvalId, true);
    // The Approve click IS the user gesture getDisplayMedia needs — this must run directly from
    // here, never from the requestScreenshot message handler.
    void screenshotSession.start().then((r) => {
      if (r.ok) {
        setScreenshotGrant(true);
        setScreenshotGranted(true);
        setSharingScreen(true);
        captureAndReplyScreenshot(approval.id);
      } else {
        reply({ type: "screenshotResult", id: approval.id, ok: false, reason: r.reason });
      }
      setNonToolDeciding(approvalId, false);
      advanceAfterScreenshot(approval.id);
    });
  }

  function denyScreenshotApproval(approval: ScreenshotApproval) {
    const approvalId = screenshotApprovalQueueId(approval.id);
    setNonToolDeciding(approvalId, true);
    reply({ type: "screenshotResult", id: approval.id, ok: false, reason: "declined" });
    setNonToolDeciding(approvalId, false);
    advanceAfterScreenshot(approval.id);
  }

  function dismissRecentToolCall(toolCallId: string) {
    setRecentToolCalls((records) => records.filter((recent) => recent.record.tool_call_id !== toolCallId));
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
      <ShellChrome
        approvalsOpen={approvalsOpen}
        onApprovalsOpenChange={setApprovalsOpen}
        liveStatus={liveStatus}
        geoGranted={geoGranted}
        tracking={tracking}
        onWithdrawGeolocation={withdrawGeolocation}
        screenshotGranted={screenshotGranted}
        sharingScreen={sharingScreen}
        onWithdrawScreenshot={withdrawScreenshot}
        pendingApprovals={toolApprovals}
        geolocationApprovals={geolocationApprovals}
        screenshotApprovals={screenshotApprovals}
        decidingApprovalIds={decidingApprovalIds}
        recentToolCalls={recentToolCalls}
        onApproveTool={approveToolApproval}
        onDenyTool={denyToolApproval}
        onApproveGeolocation={approveGeolocationApproval}
        onDenyGeolocation={denyGeolocationApproval}
        onApproveScreenshot={approveScreenshotApproval}
        onDenyScreenshot={denyScreenshotApproval}
        onDismissRecentToolCall={dismissRecentToolCall}
        onOpenToolCalls={onOpenToolCalls}
      />
      <ConfirmDialog action={activeAction} onApprove={onApprove} onCancel={onCancel} />
    </>
  );
}
