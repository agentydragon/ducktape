/**
 * The persistent left sidebar (`UISHELL_SIDEBAR`, `x/agentplane/plans/task_dag.md`): a single
 * Threads list grouped by the Sandbox that hosts them, replacing the old always-visible nav row.
 * See `x/agentplane/plans/mocks/app_shell.html` for the settled layout this follows.
 */
import { ActionIcon, Switch, Text, Tooltip } from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconArchive from "@tabler/icons-react/dist/esm/icons/IconArchive.mjs";
import IconArchiveOff from "@tabler/icons-react/dist/esm/icons/IconArchiveOff.mjs";
import IconCircleX from "@tabler/icons-react/dist/esm/icons/IconCircleX.mjs";
import IconClock from "@tabler/icons-react/dist/esm/icons/IconClock.mjs";
import IconHistory from "@tabler/icons-react/dist/esm/icons/IconHistory.mjs";
import IconListCheck from "@tabler/icons-react/dist/esm/icons/IconListCheck.mjs";
import IconPlayerPause from "@tabler/icons-react/dist/esm/icons/IconPlayerPause.mjs";
import IconPlayerPlay from "@tabler/icons-react/dist/esm/icons/IconPlayerPlay.mjs";
import IconPlus from "@tabler/icons-react/dist/esm/icons/IconPlus.mjs";
import IconSettings from "@tabler/icons-react/dist/esm/icons/IconSettings.mjs";
import { useEffect, useState } from "react";
import { useLocation, useMatch, useNavigate } from "react-router";

import { archiveThread, displayableError, listThreadsWithSandboxes, type SandboxView, type ThreadView } from "./client";
import { stateDetail } from "./sandboxes";
import "./sidebar.css";
import { archivedCount, groupThreads, threadDotColor, type ThreadGroup } from "./thread_groups";

// A poll, not a push: no cross-sandbox live-update mechanism exists yet (`live.tsx`'s `useLive` is
// scoped per-sandbox). Long enough that an operator sees a harness state change within a few
// seconds, short enough it never reads as stale; see the PR description for the tradeoff this
// accepts against building a new global SSE subscription for one sidebar.
const POLL_INTERVAL_MS = 8_000;

interface ThreadsData {
  threads: ThreadView[];
  sandboxes: Record<string, SandboxView>;
}

function useThreadsWithSandboxes(): { data: ThreadsData | null; error: string | null; refresh: () => void } {
  const [data, setData] = useState<ThreadsData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let cancelled = false;
    async function refresh(): Promise<void> {
      try {
        const result = await listThreadsWithSandboxes(true);
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      } catch (reason: unknown) {
        if (!cancelled) setError(displayableError(reason));
      }
    }
    void refresh();
    const interval = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [generation]);

  return { data, error, refresh: () => setGeneration((current) => current + 1) };
}

function GroupStateIcon({ sandbox }: { sandbox: SandboxView | null }): JSX.Element {
  if (sandbox === null) {
    return (
      <Tooltip label="Sandbox deleted" withArrow>
        <span className="agentplane-sidebar-state-icon deleted">
          <IconCircleX size={13} />
        </span>
      </Tooltip>
    );
  }
  const detail = stateDetail(sandbox);
  const [kind, Icon] =
    sandbox.state === "running"
      ? (["running", IconPlayerPlay] as const)
      : sandbox.state === "suspended"
        ? (["suspended", IconPlayerPause] as const)
        : (["pending", IconClock] as const);
  return (
    <Tooltip label={detail} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <span className={`agentplane-sidebar-state-icon ${kind}`}>
        <Icon size={13} />
      </span>
    </Tooltip>
  );
}

function ThreadRow({
  thread,
  readonly,
  current,
  onOpen,
  onToggleArchived,
}: {
  thread: ThreadView;
  readonly: boolean;
  current: boolean;
  onOpen: (thread: ThreadView) => void;
  onToggleArchived: (thread: ThreadView) => void;
}): JSX.Element {
  const label = thread.name ?? thread.session_id;
  const dot = threadDotColor(thread);
  const className = [
    "agentplane-sidebar-row",
    current ? "current" : "",
    readonly ? "readonly" : "",
    thread.archived ? "archived" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div
      className={className}
      role={readonly ? undefined : "button"}
      tabIndex={readonly ? undefined : 0}
      title={readonly ? "Sandbox deleted — read only" : undefined}
      onClick={readonly ? undefined : () => onOpen(thread)}
      onKeyDown={
        readonly
          ? undefined
          : (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onOpen(thread);
              }
            }
      }
    >
      <span className={`agentplane-sidebar-dot ${dot}`} title={dot === "ok" ? "Harness running" : "Idle"} />
      <span className="agentplane-sidebar-row-name">{label}</span>
      <Tooltip label={thread.archived ? "Unarchive" : "Archive"} withArrow>
        <ActionIcon
          className="agentplane-sidebar-row-action"
          size="xs"
          variant="subtle"
          aria-label={thread.archived ? `Unarchive ${label}` : `Archive ${label}`}
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
  current,
  onOpen,
  onToggleArchived,
}: {
  group: ThreadGroup;
  current: { sandbox: string; sessionId: string } | null;
  onOpen: (thread: ThreadView) => void;
  onToggleArchived: (thread: ThreadView) => void;
}): JSX.Element {
  const deleted = group.sandbox === null;
  return (
    <div>
      <div className="agentplane-sidebar-group-label">
        <GroupStateIcon sandbox={group.sandbox} />
        <span
          className="agentplane-sidebar-group-name"
          style={
            deleted ? { textDecoration: "line-through", textDecorationColor: "var(--mantine-color-dimmed)" } : undefined
          }
        >
          {group.sandboxName}
        </span>
        <span className="agentplane-sidebar-group-count">
          {group.threads.length} thread{group.threads.length === 1 ? "" : "s"}
        </span>
      </div>
      {group.threads.map((thread) => (
        <ThreadRow
          key={thread.id}
          thread={thread}
          readonly={deleted}
          current={current !== null && current.sandbox === thread.sandbox && current.sessionId === thread.session_id}
          onOpen={onOpen}
          onToggleArchived={onToggleArchived}
        />
      ))}
    </div>
  );
}

export function Sidebar({
  settingsOpen,
  onOpenSettings,
}: {
  settingsOpen: boolean;
  onOpenSettings: () => void;
}): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const sessionRoute = useMatch("/sandboxes/:name/sessions/:sessionId");
  const [includeArchived, setIncludeArchived] = useState(false);
  const { data, error, refresh } = useThreadsWithSandboxes();

  const threads = data?.threads ?? [];
  const groups = groupThreads(threads, data?.sandboxes ?? {}, includeArchived);
  const archived = archivedCount(threads);
  const current =
    sessionRoute?.params.name !== undefined && sessionRoute.params.sessionId !== undefined
      ? {
          sandbox: decodeURIComponent(sessionRoute.params.name),
          sessionId: decodeURIComponent(sessionRoute.params.sessionId),
        }
      : null;

  async function toggleArchived(thread: ThreadView): Promise<void> {
    try {
      await archiveThread(thread.id, !thread.archived);
    } finally {
      refresh();
    }
  }

  function open(thread: ThreadView): void {
    void navigate(`/sandboxes/${encodeURIComponent(thread.sandbox)}/sessions/${encodeURIComponent(thread.session_id)}`);
  }

  return (
    <nav className="agentplane-sidebar" aria-label="Threads">
      <div className="agentplane-sidebar-header">
        <Text fw={700} size="xs" tt="uppercase" c="dimmed">
          Threads
        </Text>
        {/* Stub for UISHELL_NEWTHREAD_LANDING: the unscoped composer isn't built yet, so "+" sends
            the operator to the Sandbox list to start one the existing way. */}
        <Tooltip label="New thread" withArrow>
          <ActionIcon variant="light" aria-label="New thread" onClick={() => void navigate("/sandboxes")}>
            <IconPlus size={13} />
          </ActionIcon>
        </Tooltip>
      </div>
      <div className="agentplane-sidebar-body">
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
            current={current}
            onOpen={open}
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
        <Tooltip label="Pending approvals" withArrow>
          <ActionIcon
            variant={location.pathname === "/actions" ? "light" : "subtle"}
            aria-label="Pending approvals"
            onClick={() => void navigate("/actions")}
          >
            <IconListCheck size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Action history" withArrow>
          <ActionIcon
            variant={location.pathname === "/actions/history" ? "light" : "subtle"}
            aria-label="Action history"
            onClick={() => void navigate("/actions/history")}
          >
            <IconHistory size={15} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Settings" withArrow>
          <ActionIcon variant={settingsOpen ? "light" : "subtle"} aria-label="Settings" onClick={onOpenSettings}>
            <IconSettings size={15} />
          </ActionIcon>
        </Tooltip>
      </div>
    </nav>
  );
}
