/**
 * Threads grouped by their Sandbox, including threadless Sandboxes and retained Threads whose
 * Sandbox no longer exists: the sidebar's navigation and the landing page's whole content.
 */
import { ActionIcon, Button, Switch, Text, TextInput, Tooltip } from "@mantine/core";
import type { SandboxView, ThreadView } from "../schema";
import { IconArchive, IconArchiveOff, IconSearch } from "../icons";
import { type JSX, useState } from "react";
import { Link, useMatch, useNavigate } from "react-router";

import { archiveThread } from "./client";
import { displayableError } from "../client";
import { harnessLabel } from "../model_catalog";
import { THREAD_PAGE, useRequiredThreadsLive, useThreadListing } from "./live";
import { MarkGlyph } from "../mark_glyph";
import { sandboxStatusDetail, sandboxSummary } from "../sandbox_status";
import { SANDBOX_STATUS_MARKS } from "../status_mark";
import "./thread_list.css";
import { groupThreads, type ThreadGroup } from "./thread_groups";
import { type ThreadNotices, useThreadNotices } from "./thread_notices";
import { ThreadStatusIndicator } from "./thread_status_indicator";
import { TimeAgo } from "../time_ago";
import { archiveBlockReason, snapshotFresh, threadStatusFromSnapshot } from "./thread_status";

function GroupStateIcon({ sandbox }: { sandbox: SandboxView | null }): JSX.Element {
  if (sandbox === null) {
    return (
      <Tooltip label="Sandbox deleted" withArrow>
        <span className="agentplane-thread-list-state-icon" data-status="gone">
          <MarkGlyph mark={SANDBOX_STATUS_MARKS.gone} />
        </span>
      </Tooltip>
    );
  }
  const { kind } = sandboxSummary(sandbox);
  return (
    <Tooltip label={sandboxStatusDetail(sandbox)} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <span className="agentplane-thread-list-state-icon" data-status={kind}>
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
  detailed,
  notices,
  onOpen,
  onToggleArchived,
}: {
  thread: ThreadView;
  sandbox: SandboxView | null;
  fresh: boolean;
  current: boolean;
  detailed: boolean;
  notices: ThreadNotices;
  onOpen: (thread: ThreadView) => void;
  onToggleArchived: (thread: ThreadView) => void;
}): JSX.Element {
  const label = thread.name ?? thread.id;
  const readonly = sandbox === null;
  const status = threadStatusFromSnapshot(thread, sandbox ?? undefined, fresh);
  const archiveBlock = archiveBlockReason(thread);
  const className = [
    "agentplane-thread-list-row",
    current ? "current" : "",
    readonly ? "readonly" : "",
    thread.archived ? "archived" : "",
    status.kind === "stopped" ? "stopped" : "",
  ]
    .filter(Boolean)
    .join(" ");
  // The Archive button is inside the keyboard-activated row, so only the row itself should navigate.
  return (
    <div
      className={className}
      role="button"
      tabIndex={0}
      title={readonly ? "Sandbox deleted — read only" : undefined}
      onClick={() => onOpen(thread)}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen(thread);
        }
      }}
    >
      <ThreadStatusIndicator
        kind={status.kind}
        label={status.label}
        inboxes={notices.bySession?.get(thread.id) ?? []}
        live={notices.live}
      />
      <span className="agentplane-thread-list-row-text">
        <span className="agentplane-thread-list-row-name">{label}</span>
        {detailed && (
          <span className="agentplane-thread-list-row-detail">
            {harnessLabel(thread.harness)}
            {thread.model ? ` · ${thread.model}` : ""} ·{" "}
            {thread.last_event_at ? <TimeAgo value={thread.last_event_at} /> : "no events yet"}
          </span>
        )}
      </span>
      <Tooltip label={thread.archived ? "Unarchive" : (archiveBlock ?? "Archive")} withArrow>
        <ActionIcon
          className="agentplane-thread-list-row-action"
          size="xs"
          variant="subtle"
          disabled={!thread.archived && archiveBlock !== null}
          aria-label={
            thread.archived ? `Unarchive ${label}` : archiveBlock ? `${archiveBlock} · ${label}` : `Archive ${label}`
          }
          title={!thread.archived ? (archiveBlock ?? undefined) : undefined}
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
  detailed,
  notices,
  onNavigate,
  onOpen,
  onToggleArchived,
}: {
  group: ThreadGroup;
  fresh: boolean;
  current: string | null;
  currentSandbox: boolean;
  detailed: boolean;
  notices: ThreadNotices;
  onNavigate: () => void;
  onOpen: (thread: ThreadView) => void;
  onToggleArchived: (thread: ThreadView) => void;
}): JSX.Element {
  const deleted = group.sandbox === null;
  const suspended = group.sandbox !== null && sandboxSummary(group.sandbox).kind === "suspended";
  const groupNameStyle = deleted
    ? { textDecoration: "line-through", textDecorationColor: "var(--mantine-color-dimmed)" }
    : undefined;
  const groupLabel = (
    <>
      <GroupStateIcon sandbox={group.sandbox} />
      <span className="agentplane-thread-list-group-name" style={groupNameStyle}>
        {group.sandboxName}
      </span>
      <span className="agentplane-thread-list-group-count">
        {group.threads.length} thread{group.threads.length === 1 ? "" : "s"}
      </span>
    </>
  );
  return (
    <div>
      {deleted ? (
        <div className={`agentplane-thread-list-group-label${currentSandbox ? " current" : ""}`}>{groupLabel}</div>
      ) : (
        <Link
          className={`agentplane-thread-list-group-label agentplane-thread-list-group-link${currentSandbox ? " current" : ""}`}
          aria-current={currentSandbox ? "page" : undefined}
          // A suspended Sandbox's name is the gray of its icon rather than the link blue.
          style={suspended ? { color: SANDBOX_STATUS_MARKS.suspended.color } : undefined}
          to={`/sandboxes/${encodeURIComponent(group.sandboxName)}`}
          onClick={onNavigate}
        >
          {groupLabel}
        </Link>
      )}
      {group.threads.map((thread) => (
        <ThreadRow
          key={thread.id}
          thread={thread}
          sandbox={group.sandbox}
          fresh={fresh}
          current={current === thread.id}
          detailed={detailed}
          notices={notices}
          onOpen={onOpen}
          onToggleArchived={onToggleArchived}
        />
      ))}
    </div>
  );
}

/** The shell's one thread listing, with its search box, paging and archived switch; every
 * `ThreadList` shows and edits the same listing. */
export function ThreadList({
  detailed,
  onNavigate,
}: {
  /** Rows add the harness, model and last activity, for a pane wide enough to hold them. */
  detailed: boolean;
  /** Runs before each navigation the list starts. */
  onNavigate: () => void;
}): JSX.Element {
  const navigate = useNavigate();
  const threadRoute = useMatch("/threads/:threadId");
  const sandboxRoute = useMatch("/sandboxes/:name");
  const live = useRequiredThreadsLive();
  const { listing, setListing, searchInput, setSearchInput } = useThreadListing();
  const data = live.snapshot;
  const fresh = snapshotFresh(live);
  const [error, setError] = useState<string | null>(null);

  const threads = data?.threads ?? [];
  const current = threadRoute?.params.threadId ?? null;
  const groups = groupThreads(
    threads,
    Object.fromEntries((data?.sandboxes ?? []).map((sandbox) => [sandbox.name, sandbox])),
    listing.includeArchived,
    current
  );
  const archived = data?.archived_count ?? 0;
  const notices = useThreadNotices(threads.map((thread) => thread.id));

  async function toggleArchived(thread: ThreadView): Promise<void> {
    setError(null);
    try {
      await archiveThread(thread.id, !thread.archived);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  }

  function openThread(thread: ThreadView): void {
    onNavigate();
    void navigate(`/threads/${encodeURIComponent(thread.id)}`);
  }

  return (
    <div className="agentplane-thread-list">
      <TextInput
        size="xs"
        placeholder="Search threads"
        aria-label="Search threads"
        leftSection={<IconSearch size={12} />}
        value={searchInput}
        onChange={(event) => setSearchInput(event.currentTarget.value)}
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
          current={current}
          currentSandbox={sandboxRoute?.params.name === group.sandboxName}
          detailed={detailed}
          notices={notices}
          onNavigate={onNavigate}
          onOpen={openThread}
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
        <div className="agentplane-thread-list-archived-toggle">
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
