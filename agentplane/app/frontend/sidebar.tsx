/**
 * The persistent left sidebar: Threads grouped by their Sandbox, including threadless Sandboxes
 * and retained Threads whose Sandbox no longer exists.
 */
import { ActionIcon, Button, Switch, Text, TextInput, Tooltip } from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconArchive from "@tabler/icons-react/dist/esm/icons/IconArchive.mjs";
import IconArchiveOff from "@tabler/icons-react/dist/esm/icons/IconArchiveOff.mjs";
import IconBox from "@tabler/icons-react/dist/esm/icons/IconBox.mjs";
import IconHeartbeat from "@tabler/icons-react/dist/esm/icons/IconHeartbeat.mjs";
import IconLayoutDashboard from "@tabler/icons-react/dist/esm/icons/IconLayoutDashboard.mjs";
import IconListCheck from "@tabler/icons-react/dist/esm/icons/IconListCheck.mjs";
import IconPlus from "@tabler/icons-react/dist/esm/icons/IconPlus.mjs";
import IconSearch from "@tabler/icons-react/dist/esm/icons/IconSearch.mjs";
import IconSettings from "@tabler/icons-react/dist/esm/icons/IconSettings.mjs";
import IconX from "@tabler/icons-react/dist/esm/icons/IconX.mjs";
import { type JSX, useEffect, useState } from "react";
import { Link, useLocation, useMatch, useNavigate } from "react-router";

import { archiveThread, displayableError, type SandboxView, type ThreadView } from "./client";
import { ActionsSidebarSection } from "./actions/sidebar";
import { THREAD_PAGE, useRequiredThreadsLive, useThreadListing } from "./live";
import { MarkGlyph } from "./mark_glyph";
import { usePhoneWidth } from "./phone_width";
import { ResizeHandle, usePaneSize } from "./resize_handle";
import { sandboxStatusDetail, sandboxSummary } from "./sandbox_status";
import { SANDBOX_STATUS_MARKS } from "./status_mark";
import "./sidebar.css";
import { groupThreads, type ThreadGroup } from "./thread_groups";
import { ThreadNoticeBadge, type ThreadNotices, useThreadNotices } from "./thread_notices";
import { ThreadStatusIndicator } from "./thread_status_indicator";
import { ScrollDebugTools } from "./threads/scroll_debug_tools";
import { snapshotFresh, threadStatusFromSnapshot } from "./thread_status";

const SEARCH_PAUSE_MS = 300;

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
  notices,
  onOpen,
  onToggleArchived,
}: {
  thread: ThreadView;
  sandbox: SandboxView | null;
  fresh: boolean;
  current: boolean;
  notices: ThreadNotices;
  onOpen: (thread: ThreadView) => void;
  onToggleArchived: (thread: ThreadView) => void;
}): JSX.Element {
  const label = thread.name ?? thread.id;
  const readonly = sandbox === null;
  const status = threadStatusFromSnapshot(thread, sandbox ?? undefined, fresh);
  const harnessRunning = status.kind === "running" || status.kind === "idle" || status.kind === "turn_error";
  const className = [
    "agentplane-sidebar-row",
    current ? "current" : "",
    readonly ? "readonly" : "",
    thread.archived ? "archived" : "",
    status.kind === "stopped" ? "stopped" : "",
  ]
    .filter(Boolean)
    .join(" ");
  // TODO: Archive's Enter/Space events also trigger row navigation. Use sibling controls or scope
  // the row's keyboard handler to events targeting the row itself.
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
      <span className="agentplane-sidebar-row-mark">
        <ThreadStatusIndicator kind={status.kind} label={status.label} />
        <ThreadNoticeBadge inboxes={notices.bySession?.get(thread.id) ?? []} live={notices.live} />
      </span>
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
  currentSandbox,
  notices,
  onNavigate,
  onOpen,
  onToggleArchived,
}: {
  group: ThreadGroup;
  fresh: boolean;
  current: string | null;
  currentSandbox: boolean;
  notices: ThreadNotices;
  onNavigate: () => void;
  onOpen: (thread: ThreadView) => void;
  onToggleArchived: (thread: ThreadView) => void;
}): JSX.Element {
  const deleted = group.sandbox === null;
  const suspended = group.sandbox !== null && sandboxSummary(group.sandbox).kind === "suspended";
  return (
    <div>
      <div className={`agentplane-sidebar-group-label${currentSandbox ? " current" : ""}`}>
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
            aria-current={currentSandbox ? "page" : undefined}
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
          notices={notices}
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

/** The Thread list: search, Sandbox groups, paging and the archived toggle. It belongs to neither
 * the sidebar column nor the route, so a docked pane renders it with its own open handler. */
export function ThreadsBrowserContent({
  currentThreadId,
  currentSandboxName = null,
  onOpenThread,
  onNavigate,
}: {
  currentThreadId: string | null;
  currentSandboxName?: string | null;
  onOpenThread: (thread: ThreadView) => void;
  onNavigate: () => void;
}): JSX.Element {
  const live = useRequiredThreadsLive();
  const { listing, setListing } = useThreadListing();
  const [search, setSearch] = useState(listing.search ?? "");
  const data = live.snapshot;
  const fresh = snapshotFresh(live);
  const [error, setError] = useState<string | null>(null);

  const threads = data?.threads ?? [];
  const groups = groupThreads(
    threads,
    Object.fromEntries((data?.sandboxes ?? []).map((sandbox) => [sandbox.name, sandbox])),
    listing.includeArchived,
    currentThreadId
  );
  const archived = data?.archived_count ?? 0;
  const notices = useThreadNotices(threads.map((thread) => thread.id));

  useEffect(() => {
    // Each listing is its own stream, so typing waits for a pause before asking for one.
    const wanted = search.trim() || null;
    if (wanted === listing.search) return;
    const timer = window.setTimeout(
      () => setListing({ ...listing, search: wanted, limit: THREAD_PAGE }),
      SEARCH_PAUSE_MS
    );
    return () => window.clearTimeout(timer);
  }, [search, listing, setListing]);

  async function toggleArchived(thread: ThreadView): Promise<void> {
    setError(null);
    try {
      await archiveThread(thread.id, !thread.archived);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  }

  return (
    <div className="agentplane-sidebar-thread-list">
      <TextInput
        size="xs"
        placeholder="Search threads"
        aria-label="Search threads"
        leftSection={<IconSearch size={12} />}
        value={search}
        onChange={(event) => setSearch(event.currentTarget.value)}
      />
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
          {listing.search === null ? "No threads yet." : "No matching threads."}
        </Text>
      )}
      {groups.map((group) => (
        <ThreadGroupSection
          key={group.sandboxName}
          group={group}
          fresh={fresh}
          current={currentThreadId}
          currentSandbox={currentSandboxName === group.sandboxName}
          notices={notices}
          onNavigate={onNavigate}
          onOpen={onOpenThread}
          onToggleArchived={(thread) => void toggleArchived(thread)}
        />
      ))}
      {data?.more && (
        <Button
          variant="subtle"
          size="compact-xs"
          onClick={() => setListing({ ...listing, limit: listing.limit + THREAD_PAGE })}
        >
          Show more threads
        </Button>
      )}
      {(archived > 0 || listing.includeArchived) && (
        <div className="agentplane-sidebar-archived-toggle">
          <Text size="xs">Show archived ({archived})</Text>
          <Switch
            size="xs"
            checked={listing.includeArchived}
            onChange={(event) =>
              setListing({ ...listing, includeArchived: event.currentTarget.checked, limit: THREAD_PAGE })
            }
            aria-label="Show archived threads"
          />
        </div>
      )}
    </div>
  );
}

export function Sidebar({ settingsOpen, onOpenSettings, open, onClose }: SidebarProps): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const threadRoute = useMatch("/threads/:threadId");
  const sandboxRoute = useMatch("/sandboxes/:name");
  const pane = usePaneSize("agentplane-sidebar-width", 240, 180, 480);
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

  return (
    <nav
      className={`agentplane-sidebar${open ? " agentplane-sidebar-open" : ""}`}
      aria-label="Threads"
      style={{ width: `${pane.size}px` }}
    >
      <ResizeHandle pane={pane} axis="x" className="agentplane-sidebar-resize-handle" aria-label="Resize sidebar" />
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
        <ThreadsBrowserContent
          currentThreadId={threadRoute?.params.threadId ?? null}
          currentSandboxName={sandboxRoute?.params.name ?? null}
          onOpenThread={(thread) => goTo(`/threads/${encodeURIComponent(thread.id)}`)}
          onNavigate={closeIfPhone}
        />
        <ActionsSidebarSection onNavigate={closeIfPhone} />
      </div>
      <div className="agentplane-sidebar-footer">
        <div className="agentplane-sidebar-debug">
          <ScrollDebugTools />
        </div>
        <Tooltip label="Sandboxes" withArrow>
          <ActionIcon
            variant={location.pathname === "/sandboxes" ? "light" : "subtle"}
            aria-label="Sandboxes"
            onClick={() => goTo("/sandboxes")}
          >
            <IconBox size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="All actions" withArrow>
          <ActionIcon
            variant={location.pathname.startsWith("/actions") ? "light" : "subtle"}
            aria-label="All actions"
            onClick={() => goTo("/actions")}
          >
            <IconListCheck size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Dockable workspace" withArrow>
          <ActionIcon variant="subtle" aria-label="Dockable workspace" onClick={() => goTo("/dock")}>
            <IconLayoutDashboard size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="System status" withArrow>
          <ActionIcon
            variant={location.pathname === "/system" ? "light" : "subtle"}
            aria-label="System status"
            onClick={() => goTo("/system")}
          >
            <IconHeartbeat size={15} />
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
