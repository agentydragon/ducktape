import { Anchor } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import {
  approvalQueueItems,
  geolocationApprovalQueueId,
  makeRecentToolCall,
  toolApprovalQueueId,
  type GeolocationApproval,
  type RecentToolCall,
} from "./approval_state.ts";
import { type GeolocationOptions, type Outbound } from "@haku/console-bridge/protocol";

import { isRoutePath, parseInbound, vetOpenLink } from "./bridge.ts";
import {
  approveToolCall,
  denyToolCall,
  fetchPendingApprovals,
  launchRoutine,
  type PendingApproval,
  type ToolCallRecord,
} from "./client.ts";
import { ConfirmDialog, type Escalation } from "./confirm_dialog.tsx";
import { ShellControls, ShellDrawer } from "./console_panel.tsx";
import { GEO_PERMISSION_DENIED, GeolocationWatcher, getGeolocation } from "./geolocation.ts";
import { hasGeolocationGrant, setGeolocationGrant } from "./geolocation_grant.ts";
import { openExternal, POPUP_HINT } from "./open_external.ts";
import { toastError, toastSuccess } from "./toast.ts";
import { useToolCallEvents } from "./tool_call_events.ts";

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

export function HakuUiEmbed({
  uiUrl,
  launchAvailable,
  onOpenToolCalls,
  onOpenSettings,
}: {
  uiUrl: string;
  launchAvailable: boolean;
  onOpenToolCalls: () => void;
  onOpenSettings: () => void;
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
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedApprovalId, setSelectedApprovalId] = useState<string | null>(null);
  const [selectedRecentToolCallId, setSelectedRecentToolCallId] = useState<string | null>(null);
  const [decidingApprovalIds, setDecidingApprovalIds] = useState<string[]>([]);
  const [recentToolCalls, setRecentToolCalls] = useState<RecentToolCall[]>([]);
  // Computed once: later routeChanged mirroring must not rewrite `src` (that would
  // reload the frame); the iframe navigates itself, the console only reflects.
  const [frameSrc] = useState(() => initialFrameSrc(uiUrl, window.location.hash));
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
  // mirrored for the console panel (below). The message handler reads the authoritative grant
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

  function openApprovalQueueCompact() {
    setSelectedApprovalId(null);
    setSelectedRecentToolCallId(null);
    setDrawerOpen(true);
  }

  function setDeciding(id: string, deciding: boolean) {
    setDecidingApprovalIds((ids) =>
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
      openApprovalQueueCompact();
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
    setDeciding(toolApprovalQueueId(record.tool_call_id), false);
    setSelectedApprovalId(null);
    setSelectedRecentToolCallId(null);
  }

  function addGeolocationApproval(mode: GeolocationApproval["mode"], id: string, options?: GeolocationOptions) {
    const approval: GeolocationApproval = { id, mode, options, createdAt: new Date().toISOString() };
    const remaining = [approval, ...geolocationApprovalsRef.current.filter((existing) => existing.id !== id)];
    geolocationApprovalsRef.current = remaining;
    setGeolocationApprovals(remaining);
    openApprovalQueueCompact();
  }

  function removeGeolocationApproval(id: string): GeolocationApproval[] {
    const remaining = geolocationApprovalsRef.current.filter((approval) => approval.id !== id);
    geolocationApprovalsRef.current = remaining;
    setGeolocationApprovals(remaining);
    return remaining;
  }

  function advanceAfterGeolocation(id: string) {
    removeGeolocationApproval(id);
    setSelectedApprovalId(null);
    setSelectedRecentToolCallId(null);
  }

  function refreshToolApprovals() {
    void fetchPendingApprovals().then(
      (approvals) => applyToolApprovals(approvals),
      (e: unknown) => toastError("Couldn't load tool approvals", e)
    );
  }

  // Live tool-call signal: initial fetch on mount plus a refetch on every server WS event
  // (which the server also broadcasts to every other tab, so they refresh too).
  useToolCallEvents(refreshToolApprovals);

  // Tear down any live browser watches if the console unmounts.
  useEffect(() => () => void watcher.stopAll(), [watcher]);

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
        // approval in the non-modal drawer so Haku's UI remains usable.
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

  useEffect(() => {
    toolApprovalsRef.current = toolApprovals;
  }, [toolApprovals]);

  useEffect(() => {
    geolocationApprovalsRef.current = geolocationApprovals;
  }, [geolocationApprovals]);

  useEffect(() => {
    const activeApprovalIds = new Set(approvalQueueItems(toolApprovals, geolocationApprovals).map((item) => item.id));
    setSelectedApprovalId((selected) => (selected && activeApprovalIds.has(selected) ? selected : null));
  }, [geolocationApprovals, toolApprovals]);

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

  useEffect(() => {
    if (
      selectedRecentToolCallId &&
      !recentToolCalls.some((recent) => recent.record.tool_call_id === selectedRecentToolCallId)
    ) {
      setSelectedRecentToolCallId(null);
    }
  }, [recentToolCalls, selectedRecentToolCallId]);

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
    const approvalId = toolApprovalQueueId(approval.tool_call_id);
    setDeciding(approvalId, true);
    void approveToolCall(approval.tool_call_id)
      .then((record) => {
        finishToolDecision(record);
        toastSuccess("Tool call finished", `${record.server_id}.${record.tool_name}: ${record.status}`);
        refreshToolApprovals();
      })
      .catch((e: unknown) => {
        setDeciding(approvalId, false);
        toastError("Tool call failed", e);
        refreshToolApprovals();
      });
  }

  function denyToolApproval(approval: PendingApproval, reason?: string) {
    const approvalId = toolApprovalQueueId(approval.tool_call_id);
    setDeciding(approvalId, true);
    void denyToolCall(approval.tool_call_id, reason || "denied from console").then(
      (record) => {
        finishToolDecision(record);
        toastSuccess("Tool call denied", approval.title ?? `${approval.server_id}: ${approval.tool_name}`);
        refreshToolApprovals();
      },
      (e: unknown) => {
        setDeciding(approvalId, false);
        toastError("Couldn't deny tool call", e);
        refreshToolApprovals();
      }
    );
  }

  function approveGeolocationApproval(approval: GeolocationApproval) {
    const approvalId = geolocationApprovalQueueId(approval.id);
    setDeciding(approvalId, true);
    setGeolocationGrant(true);
    setGeoGranted(true);
    if (approval.mode === "geolocationWatch") startWatch(approval.id, approval.options);
    else geolocateAndReply(approval.id, approval.options);
    setDeciding(approvalId, false);
    advanceAfterGeolocation(approval.id);
  }

  function denyGeolocationApproval(approval: GeolocationApproval) {
    const approvalId = geolocationApprovalQueueId(approval.id);
    setDeciding(approvalId, true);
    reply({ type: "geolocationResult", id: approval.id, ok: false, code: GEO_PERMISSION_DENIED, reason: "declined" });
    setDeciding(approvalId, false);
    advanceAfterGeolocation(approval.id);
  }

  function dismissRecentToolCall(toolCallId: string) {
    setRecentToolCalls((records) => records.filter((recent) => recent.record.tool_call_id !== toolCallId));
    if (selectedRecentToolCallId === toolCallId) setSelectedRecentToolCallId(null);
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
      <ShellControls
        pendingCount={toolApprovals.length + geolocationApprovals.length}
        opened={drawerOpen}
        onToggle={() => setDrawerOpen((open) => !open)}
        geoGranted={geoGranted}
        tracking={tracking}
        onWithdrawGeolocation={withdrawGeolocation}
      />
      <ShellDrawer
        opened={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        pendingApprovals={toolApprovals}
        geolocationApprovals={geolocationApprovals}
        selectedApprovalId={selectedApprovalId}
        selectedRecentToolCallId={selectedRecentToolCallId}
        decidingApprovalIds={decidingApprovalIds}
        recentToolCalls={recentToolCalls}
        onSelectApproval={(id) => {
          setSelectedApprovalId(id);
          setSelectedRecentToolCallId(null);
          setDrawerOpen(true);
        }}
        onSelectRecentToolCall={(toolCallId) => {
          setSelectedRecentToolCallId(toolCallId);
          setSelectedApprovalId(null);
          setDrawerOpen(true);
        }}
        onApproveTool={approveToolApproval}
        onDenyTool={denyToolApproval}
        onApproveGeolocation={approveGeolocationApproval}
        onDenyGeolocation={denyGeolocationApproval}
        onDismissRecentToolCall={dismissRecentToolCall}
        onOpenToolCalls={onOpenToolCalls}
        onOpenSettings={onOpenSettings}
      />
      <ConfirmDialog action={activeAction} onApprove={onApprove} onCancel={onCancel} />
    </>
  );
}
