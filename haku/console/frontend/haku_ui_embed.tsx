import { useEffect, useRef, useState } from "react";

import {
  geolocationApprovalQueueId,
  makeRecentToolCall,
  screenshotApprovalQueueId,
  toolApprovalQueueId,
  type GeolocationApproval,
  type RecentToolCall,
  type ScreenshotApproval,
} from "./approval_state";
import { type GeolocationOptions, type Outbound } from "@haku/console-bridge/protocol";

import { isRoutePath, parseInbound, vetOpenLink } from "./bridge";
import { ConversationsPage } from "./x/conversations_page";
import { SessionFramesPage } from "./x/session_frames_page";
import { displayableError, fetchPendingApprovals, fetchToolCall, launchRoutine, type ToolCallRecord } from "./client";
import { ConfirmDialog, type Escalation } from "./confirm_dialog";
import { ShellChrome } from "./shell_chrome";
import { GEO_PERMISSION_DENIED, GeolocationWatcher, getGeolocation } from "./geolocation";
import { hasGeolocationGrant, setGeolocationGrant } from "./geolocation_grant";
import { ExternalLink } from "./link";
import { openExternal, POPUP_HINT } from "./open_external";
import {
  CONSOLE_ROOT_PATH,
  rememberEmbedPath,
  rememberedEmbedPath,
  type ConsoleNavigationView,
  type ConsoleView,
  viewForPathname,
} from "./routing";
import { ScreenshotSession } from "./screenshot_capture";
import { hasScreenshotGrant, setScreenshotGrant } from "./screenshot_grant";
import { SettingsPanel } from "./settings_panel";
import { AgentEnrollmentPanel, type EnrollmentChoice } from "./agent_enrollment_panel";
import { toastError, toastSuccess } from "./toast";
import { useToolCallDecision } from "./tool_call_decision";
import { changedConversationId, useConsoleEvents } from "./console_events";
import { redirectToOperatorLogin } from "./operator_login";
import { useOperatorSessionDeadline, useSessionExpiringSoon } from "./operator_session";
import { ToolCallsPage } from "./tool_calls_page";

// Haku's own UI service — a separate, Authentik-gated origin running in haku-sandbox — embedded as
// a sandboxed cross-origin iframe (the backend CSP frame-src permits the embed). The console never
// renders Haku's UI itself; it frames this origin and runs the trusted **bridge**: the iframe may
// `openLink`, `requestLaunch`, read location (`requestGeolocation` one-shot /
// `startGeolocationWatch` stream), or `requestScreenshot` (a real tab-capture crop, not a DOM
// serialization) via postMessage, but only the shell decides and acts (origin-checked +
// schema-validated). `allow-same-origin`/`allow-forms` are needed for the framed app's own
// Authentik auth; **no `allow-popups`** (only the shell opens links), **no `allow="fullscreen"`**,
// **no `allow="geolocation"`** (only the shell reads location, per its own consent grant; it holds
// every location watch), and **no `allow="display-capture"`** (only the shell captures screen
// content, per its own consent grant; it holds the one live capture stream). See
// docs/containment.md.

// Restore the route the console URL carries into the frame on first mount. haku-ui speaks
// History-API paths, so the console pathname mirrors the frame route directly; a legacy `#/…`
// console URL still wins when present (old bookmarks). The input is treated strictly as a validated
// path, never a URL: the src is always `uiUrl` with only its pathname replaced, so the frame origin
// stays pinned. A path survives the whole in-frame Authentik redirect chain, interactive login
// included, carried by the `rd` parameter.
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
  return viewForPathname(loc.pathname) === "embed" && !loc.pathname.startsWith(CONSOLE_ROOT_PATH)
    ? loc.pathname
    : rememberedEmbedPath();
}

/** Whether an approvals drawer opened by an incoming approval should close after the queue drains. */
export function shouldCloseAutoOpenedApprovalQueue(
  approvalsOpen: boolean,
  openedAutomatically: boolean,
  pendingApprovalCount: number
): boolean {
  return approvalsOpen && openedAutomatically && pendingApprovalCount === 0;
}

export function HakuUiEmbed({
  uiUrl,
  launchAvailable,
  view,
  agentEnrollmentId,
  agentEnrollmentInitialChoice,
  toolCallId,
  conversationId,
  sessionFramesId,
  onNavigate,
}: {
  uiUrl: string;
  launchAvailable: boolean;
  view: ConsoleView;
  agentEnrollmentId: string | null;
  agentEnrollmentInitialChoice?: EnrollmentChoice;
  toolCallId?: string | null;
  conversationId?: string | null;
  sessionFramesId?: string | null;
  onNavigate: (view: ConsoleNavigationView) => void;
}) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const viewRef = useRef(view);
  viewRef.current = view;
  const frameTitleRef = useRef("Haku");
  // The single escalation awaiting the operator's trusted confirm (a link to open or a run
  // to launch). One typed action, dispatched on its `kind` — see ConfirmDialog's Escalation.
  const [pending, setPending] = useState<Escalation | null>(null);
  const [toolApprovals, setToolApprovals] = useState<ToolCallRecord[]>([]);
  const toolApprovalsRef = useRef<ToolCallRecord[]>([]);
  const knownToolApprovalIdsRef = useRef<Set<string> | null>(null);
  const [geolocationApprovals, setGeolocationApprovals] = useState<GeolocationApproval[]>([]);
  const geolocationApprovalsRef = useRef<GeolocationApproval[]>([]);
  const [screenshotApprovals, setScreenshotApprovals] = useState<ScreenshotApproval[]>([]);
  const screenshotApprovalsRef = useRef<ScreenshotApproval[]>([]);
  const [approvalsOpen, setApprovalsOpen] = useState(false);
  const approvalsOpenedAutomaticallyRef = useRef(false);
  // A deep-linked call opens the drawer on arrival — following the link *is* the request to
  // decide it. Keyed on the id so navigating to a different call re-opens a drawer the operator
  // closed, while closing it on the same call leaves it closed.
  useEffect(() => {
    if (!toolCallId) return;
    // Following a deep link is an explicit operator gesture, so keep the drawer open even when the
    // pending queue is empty (a provenance link commonly points at an already-finished call).
    approvalsOpenedAutomaticallyRef.current = false;
    setApprovalsOpen(true);
    // A notification normally points at a pending call already in the queue. Audit/provenance links
    // may point at an older terminal call instead; fetch that exact record so the same canonical
    // deep link can render it in Recent after a reload rather than opening an empty drawer.
    void fetchToolCall(toolCallId).then(
      (record) => {
        if (record.status !== "pending_approval") addRecentToolCall(record);
      },
      (e: unknown) => toastError("Couldn't load tool call", e)
    );
  }, [toolCallId]);

  function setApprovalsOpenFromUser(open: boolean) {
    approvalsOpenedAutomaticallyRef.current = false;
    setApprovalsOpen(open);
  }
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
  const [syncError, setSyncError] = useState<string | null>(null);
  const [syncsInFlight, setSyncsInFlight] = useState(0);
  const [lastSyncAt, setLastSyncAt] = useState<Date | null>(null);

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
    setApprovalsOpen((open) => {
      if (!open) approvalsOpenedAutomaticallyRef.current = true;
      return true;
    });
  }

  function setNonToolDeciding(id: string, deciding: boolean) {
    setDecidingNonToolApprovalIds((ids) =>
      deciding ? (ids.includes(id) ? ids : [...ids, id]) : ids.filter((existing) => existing !== id)
    );
  }

  function applyToolApprovals(approvals: ToolCallRecord[]) {
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

  function removeToolApproval(toolCallId: string): ToolCallRecord[] {
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
    setSyncsInFlight((count) => count + 1);
    void fetchPendingApprovals()
      .then(
        (approvals) => {
          setSyncError(null);
          setLastSyncAt(new Date());
          applyToolApprovals(approvals);
        },
        (e: unknown) => setSyncError(displayableError(e))
      )
      .finally(() => setSyncsInFlight((count) => count - 1));
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
  // drives the shell's live-channel warning when the socket is down. Conversation invalidations
  // are skipped: they say nothing about the approval queue, and a streaming turn emits one of
  // them every coalescing window.
  const liveStatus = useConsoleEvents((event) => {
    if (changedConversationId(event) === null) refreshToolApprovals();
  });

  const pendingApprovalCount = toolApprovals.length + geolocationApprovals.length + screenshotApprovals.length;
  useEffect(() => {
    if (
      shouldCloseAutoOpenedApprovalQueue(approvalsOpen, approvalsOpenedAutomaticallyRef.current, pendingApprovalCount)
    ) {
      approvalsOpenedAutomaticallyRef.current = false;
      setApprovalsOpen(false);
    }
  }, [approvalsOpen, pendingApprovalCount]);

  // The session's absolute deadline, surfaced in the rail once it is near. Expiry is otherwise
  // invisible until a background request 401s and navigates the tab away mid-task.
  const sessionExpiresAt = useOperatorSessionDeadline();
  const sessionExpiringSoon = useSessionExpiringSoon(sessionExpiresAt);

  // Tear down any live browser watches / capture stream if the console unmounts.
  useEffect(() => () => void watcher.stopAll(), [watcher]);
  useEffect(() => () => screenshotSession.stop(), [screenshotSession]);

  useEffect(() => {
    document.title =
      view === "embed"
        ? frameTitleRef.current
        : view === "settings" || view === "agentEnrollment"
          ? "Settings · Haku"
          : view === "toolCalls"
            ? "Past tool calls · Haku"
            : view === "conversations"
              ? "Conversations · Haku"
              : view === "sessionFrames"
                ? "Raw frames · Haku"
                : "Not found · Haku";
  }, [view]);

  // The bridge listener stays registered for the tab's whole life, reached through a ref rather
  // than being an effect dependency. Re-subscribing whenever a handler it closes over changes would
  // open a window in which a postMessage arrives with nothing listening, silently dropping a launch
  // or geolocation reply — and the ref also keeps every handler current, which a dependency list
  // naming only some of them would not.
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
      if (viewRef.current !== "embed") {
        reply({ type: "screenshotResult", id: msg.id, ok: false, reason: "Haku UI is not visible." });
        return;
      }
      // Same grant-gate shape as geolocation, but capture ALSO needs the browser's own
      // tab-share picker (no persistent silent grant for getDisplayMedia) — so even with the
      // standing grant set, a dead session (operator stopped sharing) still needs a fresh
      // approval to re-open the picker from a real click.
      if (hasScreenshotGrant() && screenshotSession.active) captureAndReplyScreenshot(msg.id);
      else addScreenshotApproval(msg.id);
      return;
    }
    if (msg.type === "routeChanged") {
      // Mirror the route into the console's own pathname so refresh/deep-links restore the view.
      // replaceState, not pushState: the iframe's own history navigations already create
      // joint-session-history entries, so Back works via the frame. Skip while a console-own view
      // (e.g. /_console/tool-calls) holds the pathname — just remember the route for the return
      // trip.
      rememberEmbedPath(msg.path);
      if (viewForPathname(window.location.pathname) === "embed") {
        history.replaceState(null, "", msg.path);
      }
      return;
    }
    if (msg.type === "titleChanged") {
      // The frame is cross-origin, so a validated bridge message is the only way for the
      // outer tab to follow its document.title.
      frameTitleRef.current = msg.title;
      if (viewRef.current === "embed") document.title = msg.title;
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

  const onMessageRef = useRef(onMessage);
  useEffect(() => {
    onMessageRef.current = onMessage;
  });

  useEffect(() => {
    const listener = (e: MessageEvent) => onMessageRef.current(e);
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, []);

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
        toastSuccess("Haku run launched", <ExternalLink href={result.session_url}>Open session</ExternalLink>);
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

  function approveToolApproval(approval: ToolCallRecord) {
    void toolDecisions.approve(approval);
  }

  function denyToolApproval(approval: ToolCallRecord, reason?: string) {
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
    <div className="haku-console-shell">
      <ShellChrome
        view={view}
        onNavigate={onNavigate}
        approvalsOpen={approvalsOpen}
        focusedToolCallId={toolCallId ?? null}
        onApprovalsOpenChange={setApprovalsOpenFromUser}
        liveStatus={liveStatus}
        syncError={syncError}
        syncing={syncsInFlight > 0}
        lastSyncAt={lastSyncAt}
        geoGranted={geoGranted}
        tracking={tracking}
        onWithdrawGeolocation={withdrawGeolocation}
        screenshotGranted={screenshotGranted}
        sharingScreen={sharingScreen}
        onWithdrawScreenshot={withdrawScreenshot}
        sessionExpiresAt={sessionExpiresAt}
        sessionExpiringSoon={sessionExpiringSoon}
        onReauthenticate={() => redirectToOperatorLogin()}
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
      />
      <main className="haku-shell-content">
        <iframe
          ref={iframeRef}
          src={frameSrc}
          title="Haku UI"
          aria-hidden={view !== "embed"}
          tabIndex={view === "embed" ? 0 : -1}
          sandbox="allow-scripts allow-same-origin allow-forms"
          className={`haku-ui-frame ${view === "embed" ? "" : "haku-ui-frame-hidden"}`}
        />
        {view === "settings" && <SettingsPanel />}
        {view === "agentEnrollment" && agentEnrollmentId !== null && (
          <AgentEnrollmentPanel
            key={`${agentEnrollmentId}:${agentEnrollmentInitialChoice}`}
            interactionId={agentEnrollmentId}
            initialChoice={agentEnrollmentInitialChoice}
            onReturnToSettings={() => onNavigate("settings")}
          />
        )}
        {view === "toolCalls" && <ToolCallsPage />}
        {view === "conversations" && <ConversationsPage conversationId={conversationId ?? null} />}
        {view === "sessionFrames" && sessionFramesId != null && <SessionFramesPage sessionId={sessionFramesId} />}
        {view === "notFound" && (
          <section className="haku-page" aria-label="Not found">
            <div className="haku-page-list">
              <h1>Page not found</h1>
              <p>This path is reserved for Haku Console, but it does not name a console page.</p>
            </div>
          </section>
        )}
      </main>
      <ConfirmDialog action={activeAction} onApprove={onApprove} onCancel={onCancel} />
    </div>
  );
}
