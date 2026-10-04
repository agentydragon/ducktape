/**
 * The persistent left sidebar: Threads grouped by their Sandbox, including threadless Sandboxes
 * and retained Threads whose Sandbox no longer exists.
 */
import { ActionIcon, Alert, Switch, Text, Tooltip } from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconArchive from "@tabler/icons-react/dist/esm/icons/IconArchive.mjs";
import IconArchiveOff from "@tabler/icons-react/dist/esm/icons/IconArchiveOff.mjs";
import IconBox from "@tabler/icons-react/dist/esm/icons/IconBox.mjs";
import IconListCheck from "@tabler/icons-react/dist/esm/icons/IconListCheck.mjs";
import IconPlus from "@tabler/icons-react/dist/esm/icons/IconPlus.mjs";
import IconSettings from "@tabler/icons-react/dist/esm/icons/IconSettings.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import { type JSX, useEffect, useRef, useState, type PointerEvent } from "react";
import { Link, useLocation, useMatch, useNavigate } from "react-router";

import { archiveThread, displayableError, type SandboxView, type ThreadView } from "./client";
import { LiveStatus, useRequiredThreadsLive, type Live, type ThreadsSnapshot } from "./live";
import { MarkGlyph } from "./mark_glyph";
import { sandboxStatusDetail, sandboxSummary } from "./sandbox_status";
import { SANDBOX_STATUS_MARKS } from "./status_mark";
import "./sidebar.css";
import { ConnectionIndicator } from "./stream_status";
import { archivedCount, groupThreads, type ThreadGroup } from "./thread_groups";
import { ThreadStatusIndicator } from "./thread_status_indicator";
import { snapshotFresh, threadStatusFromSnapshot } from "./thread_status";

const SIDEBAR_WIDTH_STORAGE_KEY = "agentplane-sidebar-width";
const SIDEBAR_DEFAULT_WIDTH = 240;
const SIDEBAR_MIN_WIDTH = 180;
const SIDEBAR_MAX_WIDTH = 480;
const SIDEBAR_KEYBOARD_STEP = 16;

function clampSidebarWidth(width: number): number {
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, width));
}

function readStoredSidebarWidth(): number {
  try {
    const stored = window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY);
    const parsed = stored === null ? NaN : Number(stored);
    return Number.isFinite(parsed) ? clampSidebarWidth(parsed) : SIDEBAR_DEFAULT_WIDTH;
  } catch (reason: unknown) {
    // A private window or blocked site data still just falls back to the default silently in
    // the UI -- this is a per-viewer convenience, not state worth an error banner over.
    console.warn("sidebar: failed to read stored width", reason);
    return SIDEBAR_DEFAULT_WIDTH;
  }
}

function writeStoredSidebarWidth(width: number): void {
  try {
    window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(width));
  } catch (reason: unknown) {
    console.warn("sidebar: failed to persist width", reason);
  }
}

/** The sidebar's user-resized width: a per-viewer convenience persisted to `localStorage`, not
 * shared state -- a fresh viewer, another device, or a private window just gets the default.
 * `resizeBy` uses React's functional state update rather than reading the latest `width`, so two
 * key-repeat steps landing in the same batch still both apply instead of the second clobbering
 * the first with a stale base. */
function useSidebarWidth(): { width: number; setWidth: (width: number) => void; resizeBy: (delta: number) => void } {
  const [width, setWidthState] = useState(readStoredSidebarWidth);

  function setWidth(next: number): void {
    const clamped = clampSidebarWidth(next);
    setWidthState(clamped);
    writeStoredSidebarWidth(clamped);
  }

  function resizeBy(delta: number): void {
    setWidthState((current) => {
      const clamped = clampSidebarWidth(current + delta);
      writeStoredSidebarWidth(clamped);
      return clamped;
    });
  }

  return { width, setWidth, resizeBy };
}

/** Drag (pointer) or arrow-key (keyboard) resize handle on the sidebar's trailing edge. `onDrag`
 * sets an absolute width computed from the drag's own start point; `onStep` nudges by a relative
 * amount, since arrow-key repeats can land faster than a re-render carries the new `width` prop
 * back in. */
function SidebarResizeHandle({
  width,
  onDrag,
  onStep,
}: {
  width: number;
  onDrag: (width: number) => void;
  onStep: (delta: number) => void;
}): JSX.Element {
  const dragRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);

  function onPointerDown(event: PointerEvent<HTMLDivElement>): void {
    dragRef.current = { pointerId: event.pointerId, startX: event.clientX, startWidth: width };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function onPointerMove(event: PointerEvent<HTMLDivElement>): void {
    const drag = dragRef.current;
    if (drag === null || drag.pointerId !== event.pointerId) return;
    onDrag(drag.startWidth + (event.clientX - drag.startX));
  }

  function onPointerUp(event: PointerEvent<HTMLDivElement>): void {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  return (
    <div
      className="agentplane-sidebar-resize-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sidebar"
      aria-valuenow={width}
      aria-valuemin={SIDEBAR_MIN_WIDTH}
      aria-valuemax={SIDEBAR_MAX_WIDTH}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft") onStep(-SIDEBAR_KEYBOARD_STEP);
        else if (event.key === "ArrowRight") onStep(SIDEBAR_KEYBOARD_STEP);
      }}
    />
  );
}

// Matches sidebar.css's own phone breakpoint.
const PHONE_QUERY = "(max-width: 560px)";

/** Whether the sidebar is currently rendering as the phone-width full-screen overlay rather than a
 * docked desktop column -- the two need different close-on-navigate behavior below. */
function usePhoneWidth(): boolean {
  const [phone, setPhone] = useState(() => window.matchMedia(PHONE_QUERY).matches);
  useEffect(() => {
    const query = window.matchMedia(PHONE_QUERY);
    const onChange = (event: MediaQueryListEvent): void => setPhone(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return phone;
}

function GroupStateIcon({ sandbox }: { sandbox: SandboxView | null }): JSX.Element {
  if (sandbox === null) {
    return (
      <Tooltip label="Sandbox deleted" withArrow>
        <span className="agentplane-sidebar-state-icon" data-status="gone">
          <MarkGlyph mark={SANDBOX_STATUS_MARKS.gone} />
        </span>
      </Tooltip>
    );
  }
  const { kind } = sandboxSummary(sandbox);
  return (
    <Tooltip label={sandboxStatusDetail(sandbox)} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <span className="agentplane-sidebar-state-icon" data-status={kind}>
        <MarkGlyph mark={SANDBOX_STATUS_MARKS[kind]} />
      </span>
    </Tooltip>
  );
}

function ThreadRow({
  thread,
  sandbox,
  fresh,
  current,
  onOpen,
  onToggleArchived,
}: {
  thread: ThreadView;
  sandbox: SandboxView | null;
  fresh: boolean;
  current: boolean;
  onOpen: (thread: ThreadView) => void;
  onToggleArchived: (thread: ThreadView) => void;
}): JSX.Element {
  const label = thread.name ?? thread.session_id;
  const readonly = sandbox === null;
  const status = threadStatusFromSnapshot(thread, sandbox ?? undefined, fresh);
  const harnessRunning = status.kind === "running" || status.kind === "idle";
  const className = [
    "agentplane-sidebar-row",
    current ? "current" : "",
    readonly ? "readonly" : "",
    thread.archived ? "archived" : "",
    status.kind === "stopped" ? "stopped" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div
      className={className}
      role="button"
      tabIndex={0}
      title={readonly ? "Sandbox deleted — read only" : undefined}
      onClick={() => onOpen(thread)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen(thread);
        }
      }}
    >
      <ThreadStatusIndicator kind={status.kind} label={status.label} size="small" />
      <span className="agentplane-sidebar-row-name">{label}</span>
      <Tooltip
        label={thread.archived ? "Unarchive" : harnessRunning ? "Stop the harness before archiving" : "Archive"}
        withArrow
      >
        <ActionIcon
          className="agentplane-sidebar-row-action"
          size="xs"
          variant="subtle"
          disabled={!thread.archived && harnessRunning}
          aria-label={
            thread.archived
              ? `Unarchive ${label}`
              : harnessRunning
                ? `Stop harness before archiving ${label}`
                : `Archive ${label}`
          }
          title={!thread.archived && harnessRunning ? "Stop the harness before archiving" : undefined}
          onClick={(event) => {
            event.stopPropagation();
            onToggleArchived(thread);
          }}
        >
          {thread.archived ? <IconArchiveOff size={11} /> : <IconArchive size={11} />}
        </ActionIcon>
      </Tooltip>
    </div>
  );
}

function ThreadGroupSection({
  group,
  fresh,
  current,
  onNavigate,
  onOpen,
  onToggleArchived,
}: {
  group: ThreadGroup;
  fresh: boolean;
  current: string | null;
  onNavigate: () => void;
  onOpen: (thread: ThreadView) => void;
  onToggleArchived: (thread: ThreadView) => void;
}): JSX.Element {
  const deleted = group.sandbox === null;
  const suspended = group.sandbox !== null && sandboxSummary(group.sandbox).kind === "suspended";
  return (
    <div>
      <div className="agentplane-sidebar-group-label">
        <GroupStateIcon sandbox={group.sandbox} />
        {deleted ? (
          <span
            className="agentplane-sidebar-group-name"
            style={{ textDecoration: "line-through", textDecorationColor: "var(--mantine-color-dimmed)" }}
          >
            {group.sandboxName}
          </span>
        ) : (
          <Link
            className="agentplane-sidebar-group-name agentplane-sidebar-group-link"
            // A suspended Sandbox's name is the gray of its icon rather than the link blue.
            style={suspended ? { color: SANDBOX_STATUS_MARKS.suspended.color } : undefined}
            to={`/sandboxes/${encodeURIComponent(group.sandboxName)}`}
            onClick={onNavigate}
          >
            {group.sandboxName}
          </Link>
        )}
        <span className="agentplane-sidebar-group-count">
          {group.threads.length} thread{group.threads.length === 1 ? "" : "s"}
        </span>
      </div>
      {group.threads.map((thread) => (
        <ThreadRow
          key={thread.id}
          thread={thread}
          sandbox={group.sandbox}
          fresh={fresh}
          current={current === thread.id}
          onOpen={onOpen}
          onToggleArchived={onToggleArchived}
        />
      ))}
    </div>
  );
}

type SidebarProps = {
  settingsOpen: boolean;
  onOpenSettings: () => void;
  /** A phone-width overlay, or a docked column on desktop. */
  open: boolean;
  onClose: () => void;
};

export function Sidebar(props: SidebarProps): JSX.Element {
  return <SidebarView {...props} live={useRequiredThreadsLive()} />;
}

function SidebarView({
  settingsOpen,
  onOpenSettings,
  open,
  onClose,
  live,
}: SidebarProps & { live: Live<ThreadsSnapshot> }): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const threadRoute = useMatch("/threads/:threadId");
  const [includeArchived, setIncludeArchived] = useState(false);
  const { width, setWidth, resizeBy } = useSidebarWidth();
  const data = live.snapshot;
  const fresh = snapshotFresh(live);
  const [error, setError] = useState<string | null>(null);
  const phone = usePhoneWidth();

  useEffect(() => {
    // Escape dismisses the phone-width overlay, the way it would any other full-screen modal.
    // At desktop width there's nothing modal to dismiss -- collapsing the persistent column would
    // just be a surprising side effect of a keypress unrelated to the sidebar.
    if (!open || !phone) return;
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, phone, onClose]);

  const threads = data?.threads ?? [];
  const groups = groupThreads(
    threads,
    Object.fromEntries((data?.sandboxes ?? []).map((sandbox) => [sandbox.name, sandbox])),
    includeArchived
  );
  const archived = archivedCount(threads);
  const current = threadRoute?.params.threadId ?? null;

  async function toggleArchived(thread: ThreadView): Promise<void> {
    setError(null);
    try {
      await archiveThread(thread.id, !thread.archived);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  }

  /** Every navigation out of the sidebar also closes it, but only at phone width, where it's a
   * full-screen overlay standing in the way of the page it just navigated to. At desktop width
   * this is a no-op: collapsing the persistent column on every click would undo the reader's own
   * choice to keep it open just because they used it. */
  function closeIfPhone(): void {
    if (phone) onClose();
  }

  function goTo(path: string): void {
    closeIfPhone();
    void navigate(path);
  }

  function openThread(thread: ThreadView): void {
    goTo(`/threads/${encodeURIComponent(thread.id)}`);
  }

  return (
    <nav
      className={`agentplane-sidebar${open ? " agentplane-sidebar-open" : ""}`}
      aria-label="Threads"
      style={{ width: `${width}px` }}
    >
      <SidebarResizeHandle width={width} onDrag={setWidth} onStep={resizeBy} />
      <div className="agentplane-sidebar-header">
        <Text fw={700} size="xs" tt="uppercase" c="dimmed">
          Threads
        </Text>
        <div style={{ display: "flex", gap: 2, alignItems: "center" }}>
          {/* The unscoped new-thread composer isn't built yet, so "+" sends the operator to the
              Sandbox list to start one the existing way -- label says exactly that rather than
              promising a composer that isn't there yet. */}
          <Tooltip label="New thread (via Sandboxes)" withArrow>
            <ActionIcon variant="light" aria-label="New thread (via Sandboxes)" onClick={() => goTo("/sandboxes")}>
              <IconPlus size={13} />
            </ActionIcon>
          </Tooltip>
          {/* Only shown by sidebar.css's phone-width, drawer-open state: at desktop width the
              always-visible shell topbar's own toggle already collapses this column. */}
          <ActionIcon
            className="agentplane-sidebar-close"
            variant="subtle"
            aria-label="Close navigation"
            onClick={onClose}
          >
            <IconX size={16} />
          </ActionIcon>
        </div>
      </div>
      <div className="agentplane-sidebar-body">
        <LiveStatus live={live} />
        {data?.updates_connected === false && (
          <Alert color="orange" p="xs">
            Thread updates disconnected; showing the last snapshot.
          </Alert>
        )}
        {error && (
          <Text c="red" size="xs" px={4}>
            {error}
          </Text>
        )}
        {data === null && !error && (
          <Text c="dimmed" size="xs" px={4}>
            Loading threads…
          </Text>
        )}
        {data !== null && groups.length === 0 && (
          <Text c="dimmed" size="xs" px={4}>
            No threads yet.
          </Text>
        )}
        {groups.map((group) => (
          <ThreadGroupSection
            key={group.sandboxName}
            group={group}
            fresh={fresh}
            current={current}
            onNavigate={closeIfPhone}
            onOpen={openThread}
            onToggleArchived={(thread) => void toggleArchived(thread)}
          />
        ))}
        {threads.length > 0 && (
          <div className="agentplane-sidebar-archived-toggle">
            <Text size="xs">Show archived ({archived})</Text>
            <Switch
              size="xs"
              checked={includeArchived}
              onChange={(event) => setIncludeArchived(event.currentTarget.checked)}
              aria-label="Show archived threads"
            />
          </div>
        )}
      </div>
      <div className="agentplane-sidebar-footer">
        <ConnectionIndicator />
        <Tooltip label="Sandboxes" withArrow>
          <ActionIcon
            variant={location.pathname === "/sandboxes" ? "light" : "subtle"}
            aria-label="Sandboxes"
            onClick={() => goTo("/sandboxes")}
          >
            <IconBox size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Actions" withArrow>
          <ActionIcon
            variant={location.pathname === "/actions" ? "light" : "subtle"}
            aria-label="Actions"
            onClick={() => goTo("/actions")}
          >
            <IconListCheck size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Settings" withArrow>
          <ActionIcon
            variant={settingsOpen ? "light" : "subtle"}
            aria-label="Settings"
            onClick={() => {
              closeIfPhone();
              onOpenSettings();
            }}
          >
            <IconSettings size={15} />
          </ActionIcon>
        </Tooltip>
      </div>
    </nav>
  );
}
