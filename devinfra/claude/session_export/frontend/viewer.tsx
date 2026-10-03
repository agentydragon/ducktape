import {
  createContext,
  useContext,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type JSX,
  type ReactNode,
} from "react";
import {
  Accordion,
  Alert,
  Badge,
  Button,
  Box,
  Center,
  Code,
  Drawer,
  Group,
  Image,
  Loader,
  NavLink,
  Paper,
  Progress,
  ScrollArea,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Title,
  UnstyledButton,
} from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";

import {
  ApiError,
  getSession,
  listSessionEvents,
  listSessions,
  watchSessions,
  type SessionEvent,
  type SessionSummary,
} from "./api";
import { EventInspector } from "./event-inspector";
import { groupToolActivity, toolGroupSummary, type ToolGroup } from "./tool-groups";
import {
  foldSessionEvents,
  transcriptEventTime,
  type TranscriptItem,
  type TranscriptToolCall,
  type TranscriptToolRun,
} from "./transcript";

type StatusFilter = "all" | "active" | "paused" | "archived";
type WatchStatus = "connecting" | "connected" | "reconnecting";
const ALL_STATUSES = ["active", "paused", "archived"];
const SIDEBAR_MIN_WIDTH = 220;
const SIDEBAR_MAX_WIDTH = 480;
const SIDEBAR_DEFAULT_WIDTH = 300;
const TRANSCRIPT_MIN_WIDTH = 420;
const SIDEBAR_RESIZER_WIDTH = 8;
const SIDEBAR_WIDTH_STORAGE_KEY = "claude-session-sidebar-width";
const SIDEBAR_VISIBLE_STORAGE_KEY = "claude-session-sidebar-visible";

function clampSidebarWidth(width: number): number {
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, width));
}

function readSidebarWidth(): number {
  try {
    const stored = window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY);
    const width = stored === null ? SIDEBAR_DEFAULT_WIDTH : Number(stored);
    return Number.isFinite(width) ? clampSidebarWidth(width) : SIDEBAR_DEFAULT_WIDTH;
  } catch {
    return SIDEBAR_DEFAULT_WIDTH;
  }
}

function readSidebarVisibility(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_VISIBLE_STORAGE_KEY) !== "false";
  } catch {
    return true;
  }
}

function storeSidebarPreference(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Session browsing still works when the browser disables local storage.
  }
}

function maxSidebarWidth(layoutWidth: number): number {
  return Math.max(
    SIDEBAR_MIN_WIDTH,
    Math.min(SIDEBAR_MAX_WIDTH, layoutWidth - TRANSCRIPT_MIN_WIDTH - SIDEBAR_RESIZER_WIDTH)
  );
}

type TranscriptScrollIntent =
  | { kind: "tail"; sessionId: string }
  | {
      kind: "anchor";
      sessionId: string;
      element: HTMLElement | null;
      sequences: string[];
      top: number;
      scrollTop: number;
      scrollHeight: number;
    };

function sequenceOrder(left: SessionEvent, right: SessionEvent): number {
  const leftSequence = BigInt(left.sequence_num);
  const rightSequence = BigInt(right.sequence_num);
  return leftSequence < rightSequence ? -1 : leftSequence > rightSequence ? 1 : 0;
}

function mergeSessionEvents(previous: SessionEvent[], incoming: SessionEvent[]): SessionEvent[] {
  if (incoming.length === 0) return previous;
  const bySequence = new Map(previous.map((event) => [event.sequence_num, event]));
  let changed = false;
  for (const event of incoming) {
    const existing = bySequence.get(event.sequence_num);
    if (existing === undefined || JSON.stringify(existing) !== JSON.stringify(event)) {
      bySequence.set(event.sequence_num, event);
      changed = true;
    }
  }
  return changed ? [...bySequence.values()].sort(sequenceOrder) : previous;
}

function mergeSessionPage(latest: SessionSummary[], previous: SessionSummary[]): SessionSummary[] {
  const latestIds = new Set(latest.map((session) => session.id));
  return [...latest, ...previous.filter((session) => !latestIds.has(session.id))];
}

function sessionMatchesFilter(session: SessionSummary, filter: StatusFilter): boolean {
  return filter === "all" || session.status === filter;
}

function sortSessions(sessions: SessionSummary[]): SessionSummary[] {
  return [...sessions].sort((left, right) => {
    // Match PostgreSQL's DESC ordering (NULLS FIRST), then the API's stable ID tie-breaker.
    const leftTime = left.last_event_at === null ? Number.POSITIVE_INFINITY : Date.parse(left.last_event_at);
    const rightTime = right.last_event_at === null ? Number.POSITIVE_INFINITY : Date.parse(right.last_event_at);
    const byTimestamp = rightTime - leftTime;
    return (Number.isNaN(byTimestamp) ? 0 : byTimestamp) || right.id.localeCompare(left.id);
  });
}

function retryable(reason: unknown): boolean {
  if (reason instanceof ApiError) return reason.status === 408 || reason.status === 429 || reason.status >= 500;
  return reason instanceof TypeError;
}

function waitForRetry(delay: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(new DOMException("Request aborted", "AbortError"));
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, delay);
    const abort = (): void => {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Request aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}

async function retryTransient<T>(request: () => Promise<T>, signal: AbortSignal): Promise<T> {
  let delay = 500;
  while (!signal.aborted) {
    try {
      return await request();
    } catch (reason) {
      if (!retryable(reason)) throw reason;
      await waitForRetry(delay, signal);
      delay = Math.min(delay * 2, 30_000);
    }
  }
  throw new DOMException("Request aborted", "AbortError");
}

function errorMessage(reason: unknown): string {
  return reason instanceof ApiError || reason instanceof Error ? reason.message : "Could not load sessions.";
}

function sessionSubtitle(session: SessionSummary): string {
  const fields = session as SessionSummary & { git_branch?: unknown; repo_path?: unknown; repository?: unknown };
  const branch = typeof fields.git_branch === "string" ? fields.git_branch : null;
  const repository =
    typeof fields.repository === "string"
      ? fields.repository
      : typeof fields.repo_path === "string"
        ? fields.repo_path
        : null;
  return [repository, branch].filter((value): value is string => value !== null).join(" · ") || session.id;
}

function statusColor(status: string): "green" | "yellow" | "gray" {
  if (status === "active") return "green";
  if (status === "paused") return "yellow";
  return "gray";
}

function SessionRow({
  session,
  selected,
  onSelect,
}: {
  session: SessionSummary;
  selected: boolean;
  onSelect: () => void;
}): JSX.Element {
  return (
    <NavLink
      component="button"
      type="button"
      active={selected}
      aria-pressed={selected}
      onClick={onSelect}
      label={
        <Text size="sm" fw={600} truncate>
          {session.title || "Untitled session"}
        </Text>
      }
      description={
        <Stack gap={4} mt={6}>
          <Group gap="xs">
            <Badge size="xs" variant="light" color={statusColor(session.status)}>
              {session.status}
            </Badge>
            <Text component="time" size="xs" c="dimmed" dateTime={session.updated_at}>
              {new Date(session.updated_at).toLocaleDateString()}
            </Text>
          </Group>
          <Text size="xs" c="dimmed" ff="monospace" truncate>
            {sessionSubtitle(session)}
          </Text>
        </Stack>
      }
    />
  );
}

function SessionList({
  id,
  sessions,
  visibleSessions,
  selectedId,
  loadingSessions,
  nextSessionCursor,
  loadingMoreSessions,
  search,
  filter,
  onSearchChange,
  onFilterChange,
  onSelect,
  onLoadMore,
}: {
  id: string;
  sessions: SessionSummary[];
  visibleSessions: SessionSummary[];
  selectedId: string | null;
  loadingSessions: boolean;
  nextSessionCursor: string | null;
  loadingMoreSessions: boolean;
  search: string;
  filter: StatusFilter;
  onSearchChange: (search: string) => void;
  onFilterChange: (filter: StatusFilter) => void;
  onSelect: (sessionId: string) => void;
  onLoadMore: () => void;
}): JSX.Element {
  return (
    <Paper
      id={id}
      component="aside"
      aria-label="Session list"
      withBorder
      radius="sm"
      p="xs"
      style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, overflow: "hidden" }}
    >
      <Stack gap="xs" style={{ flex: 1, minHeight: 0 }}>
        <TextInput
          label="Search loaded sessions"
          type="search"
          value={search}
          onChange={(event) => onSearchChange(event.currentTarget.value)}
        />
        <Select
          label="Status"
          data={[
            { value: "all", label: "All sessions" },
            { value: "active", label: "Active" },
            { value: "paused", label: "Paused" },
            { value: "archived", label: "Archived" },
          ]}
          value={filter}
          onChange={(value) => {
            if (value !== null) onFilterChange(value as StatusFilter);
          }}
        />
        <ScrollArea style={{ flex: 1, minHeight: 0 }} type="auto">
          {loadingSessions ? (
            <Center h={240}>
              <Loader size="sm" aria-label="Loading sessions" />
            </Center>
          ) : visibleSessions.length === 0 ? (
            <Center h={180} px="md">
              <Text c="dimmed" ta="center">
                {sessions.length === 0 ? "No sessions in the sync yet." : "No matching sessions."}
              </Text>
            </Center>
          ) : (
            <Stack gap={4}>
              {visibleSessions.map((session) => (
                <SessionRow
                  key={session.id}
                  session={session}
                  selected={session.id === selectedId}
                  onSelect={() => onSelect(session.id)}
                />
              ))}
            </Stack>
          )}
          {nextSessionCursor !== null && (
            <Button fullWidth variant="default" mt="xs" loading={loadingMoreSessions} onClick={onLoadMore}>
              Load more sessions
            </Button>
          )}
        </ScrollArea>
      </Stack>
    </Paper>
  );
}

function inputRecord(input: unknown): Record<string, unknown> | null {
  return typeof input === "object" && input !== null && !Array.isArray(input)
    ? (input as Record<string, unknown>)
    : null;
}

function toolAction(tool: TranscriptToolCall): { label: string; value: string; code: boolean } | null {
  const input = inputRecord(tool.input);
  if (typeof tool.input === "string") return { label: "Input", value: tool.input, code: true };
  if (input === null) return null;

  const field = (key: string): string | null => (typeof input[key] === "string" ? (input[key] as string) : null);
  if (tool.name === "Bash" || tool.name === "bash") {
    const command = field("command");
    if (command !== null) return { label: "Command", value: command, code: true };
  }
  if (["Read", "Write", "Edit", "MultiEdit", "NotebookEdit"].includes(tool.name)) {
    const path = field("file_path") ?? field("path");
    if (path !== null) return { label: "File", value: path, code: true };
  }
  if (tool.name === "Grep" || tool.name === "Glob") {
    const pattern = field("pattern");
    const path = field("path") ?? field("glob");
    if (pattern !== null || path !== null) {
      return {
        label: tool.name === "Grep" ? "Search" : "Match",
        value: [pattern, path].filter(Boolean).join(" · "),
        code: true,
      };
    }
  }
  if (tool.name === "WebSearch") {
    const query = field("query");
    if (query !== null) return { label: "Search", value: query, code: false };
  }
  if (tool.name === "WebFetch") {
    const url = field("url");
    if (url !== null) return { label: "URL", value: url, code: true };
  }
  if (tool.name === "Task" || tool.name === "Agent") {
    const description = field("description") ?? field("name") ?? field("prompt");
    if (description !== null) return { label: "Task", value: description, code: false };
  }
  for (const [key, label] of [
    ["command", "Command"],
    ["file_path", "File"],
    ["path", "Path"],
    ["query", "Query"],
    ["pattern", "Pattern"],
    ["url", "URL"],
    ["description", "Description"],
  ]) {
    const value = field(key);
    if (value !== null)
      return { label, value, code: label === "Command" || label === "File" || label === "Path" || label === "URL" };
  }
  return null;
}

function toolRunPreview(item: TranscriptToolRun): string {
  const previews = item.tools.slice(0, 3).map((tool) => {
    const action = toolAction(tool);
    const firstLine = action?.value.split("\n")[0]?.trim();
    const summary = firstLine === undefined || firstLine === "" ? tool.name : firstLine;
    const shortSummary = summary.length > 120 ? `${summary.slice(0, 117)}…` : summary;
    return item.tools.length === 1 || firstLine === undefined || firstLine === ""
      ? shortSummary
      : `${tool.name}: ${shortSummary}`;
  });
  if (item.tools.length > 3) previews.push(`+${item.tools.length - 3} more`);
  return previews.join(" · ");
}

function toolArguments(tool: TranscriptToolCall): string | null {
  if (tool.input === null || tool.input === undefined) return null;
  return typeof tool.input === "string" ? tool.input : JSON.stringify(tool.input, null, 2);
}

function toolStatusColor(status: string): "green" | "yellow" | "red" | "gray" {
  if (status === "complete" || status === "completed") return "green";
  if (status === "running") return "yellow";
  if (status === "error" || status === "denied" || status === "failed") return "red";
  return "gray";
}

function ToolRun({ item }: { item: TranscriptToolRun }): JSX.Element {
  return (
    <Stack gap="sm">
      {item.tools.map((tool, index) => {
        const action = toolAction(tool);
        const args = toolArguments(tool);
        return (
          <Paper
            key={tool.toolUseId}
            withBorder
            radius="sm"
            p="sm"
            data-tool-name={tool.name}
            data-parent-tool-use-id={tool.parentToolUseId}
          >
            <Stack gap="xs">
              <Group justify="space-between" align="center" gap="xs">
                <Group gap="xs">
                  <Text size="sm" fw={600}>
                    {tool.name}
                  </Text>
                  <Badge variant="dot" color={toolStatusColor(tool.status)}>
                    {tool.status}
                  </Badge>
                  {tool.policyDenied && (
                    <Badge color="red" variant="light">
                      Permission denied
                    </Badge>
                  )}
                </Group>
                <Text size="xs" c="dimmed">
                  {index + 1} of {item.tools.length}
                </Text>
              </Group>
              {action !== null && (
                <Stack gap={4}>
                  <Text size="xs" c="dimmed">
                    {action.label}
                  </Text>
                  {action.code ? (
                    <Code block>{action.value}</Code>
                  ) : (
                    <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                      {action.value}
                    </Text>
                  )}
                </Stack>
              )}
              {tool.progress !== undefined && (
                <Text size="sm" c="dimmed">
                  {tool.progress}
                </Text>
              )}
              {tool.summary !== undefined && <Text size="sm">{tool.summary}</Text>}
              {(tool.name === "Task" || tool.name === "Agent") &&
                tool.status === "running" &&
                tool.subagentActivity !== undefined && (
                  <Paper
                    withBorder
                    radius="xs"
                    p="xs"
                    data-subagent-activity
                    data-subagent-tool-count={tool.subagentActivity.toolCallCount}
                  >
                    <Group gap="xs">
                      <Text size="xs" fw={500}>
                        Agent activity
                      </Text>
                      <Badge
                        size="xs"
                        variant="light"
                        color="blue"
                        data-subagent-latest-tool={tool.subagentActivity.latestToolName}
                      >
                        {tool.subagentActivity.latestToolName}
                      </Badge>
                      <Badge size="xs" variant="light" color="gray">
                        {tool.subagentActivity.toolCallCount} calls
                      </Badge>
                    </Group>
                    {tool.subagentActivity.model !== undefined && (
                      <Text size="xs" c="dimmed" mt={4}>
                        {tool.subagentActivity.model}
                      </Text>
                    )}
                  </Paper>
                )}
              {(tool.result !== undefined || (tool.outputImages?.length ?? 0) > 0) && (
                <Stack gap={4}>
                  <Text size="xs" c="dimmed">
                    {tool.filePreview !== undefined
                      ? "File contents"
                      : tool.failed
                        ? "Error output"
                        : tool.name === "Bash"
                          ? "Command output"
                          : "Output"}
                  </Text>
                  {tool.filePreview !== undefined ? (
                    <Stack gap={4} data-tool-file-preview>
                      <Text size="xs" ff="monospace" c="dimmed" data-tool-file-path={tool.filePreview.path}>
                        {tool.filePreview.path}
                      </Text>
                      <Code
                        block
                        style={{ maxHeight: 240, overflow: "auto", whiteSpace: "pre-wrap" }}
                        data-tool-file-content
                      >
                        {tool.filePreview.contents || "(empty file)"}
                      </Code>
                    </Stack>
                  ) : tool.result !== undefined ? (
                    <Code block style={{ maxHeight: 240, overflow: "auto", whiteSpace: "pre-wrap" }}>
                      {tool.result}
                    </Code>
                  ) : null}
                  {tool.outputImages?.map((outputImage, imageIndex) => (
                    <Image
                      key={`${outputImage.mimeType}-${imageIndex}`}
                      src={`data:${outputImage.mimeType};base64,${outputImage.data}`}
                      alt={`Tool output ${imageIndex + 1}`}
                      w={240}
                      h={180}
                      fit="contain"
                      radius="sm"
                      data-tool-output-image
                    />
                  ))}
                </Stack>
              )}
              {tool.tasks.map((task) => (
                <Paper key={task.taskId} withBorder radius="xs" p="xs" bg="var(--mantine-color-default-hover)">
                  <Group justify="space-between" align="center" gap="xs">
                    <Text size="sm" fw={500}>
                      {task.title}
                    </Text>
                    <Badge size="xs" variant="light" color={toolStatusColor(task.status)}>
                      {task.status}
                    </Badge>
                  </Group>
                  {task.detail !== undefined && (
                    <Text size="xs" c="dimmed" mt={4}>
                      {task.detail}
                    </Text>
                  )}
                </Paper>
              ))}
              {args !== null && (
                <Accordion variant="default" radius="sm">
                  <Accordion.Item value="arguments">
                    <Accordion.Control>Tool input</Accordion.Control>
                    <Accordion.Panel>
                      <ScrollArea type="auto" mah={240}>
                        <Code block>{args}</Code>
                      </ScrollArea>
                    </Accordion.Panel>
                  </Accordion.Item>
                </Accordion>
              )}
            </Stack>
          </Paper>
        );
      })}
    </Stack>
  );
}

function FoldableDetail({ label, detail }: { label: string; detail: string }): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  return (
    <Stack gap={4}>
      <UnstyledButton
        w="100%"
        mih={22}
        px={4}
        aria-label={expanded ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`}
        aria-expanded={expanded}
        data-detail-toggle
        onClick={() => setExpanded((value) => !value)}
        style={{ display: "flex", alignItems: "center", gap: 4, minWidth: 0 }}
      >
        <Text component="span" size="xs" fw={600} aria-hidden="true">
          {expanded ? "−" : "+"}
        </Text>
        <Text component="span" size="xs" c="dimmed">
          {label}
        </Text>
      </UnstyledButton>
      {expanded && (
        <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {detail}
        </Text>
      )}
    </Stack>
  );
}

type ToolDisclosures = {
  tools: Set<string>;
  groups: Set<string>;
  toggleTool: (id: string) => void;
  toggleGroup: (ids: string[], expanded: boolean) => void;
};
const ToolDisclosureContext = createContext<ToolDisclosures | null>(null);

function ToolDisclosureProvider({ children }: { children: ReactNode }): JSX.Element {
  const [tools, setTools] = useState(new Set<string>());
  const [groups, setGroups] = useState(new Set<string>());
  return (
    <ToolDisclosureContext.Provider
      value={{
        tools,
        groups,
        toggleTool: (id) =>
          setTools((previous) => {
            const next = new Set(previous);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
          }),
        toggleGroup: (ids, expanded) => {
          setGroups((previous) => {
            const next = new Set(previous);
            for (const id of ids) {
              if (expanded) next.delete(id);
              else next.add(id);
            }
            return next;
          });
          if (expanded) setTools((previous) => new Set([...previous].filter((id) => !ids.includes(id))));
        },
      }}
    >
      {children}
    </ToolDisclosureContext.Provider>
  );
}

function useToolDisclosures(): ToolDisclosures {
  const state = useContext(ToolDisclosureContext);
  if (state === null) throw new Error("Tool disclosures require a provider");
  return state;
}

function CompactToolRun({ item }: { item: TranscriptToolRun }): JSX.Element {
  const disclosures = useToolDisclosures();
  const expanded = disclosures.tools.has(item.id);
  return (
    <Paper
      component="article"
      title={transcriptEventTime(item) ?? undefined}
      aria-label={item.tools.length === 1 ? `${item.tools[0]?.name ?? "Tool"} activity` : "Tool activity"}
      data-fold-kind="tool-run"
      data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
      data-tool-count={item.tools.length}
      data-parent-tool-use-id={item.parentToolUseId}
      radius="sm"
      py={2}
    >
      <UnstyledButton
        w="100%"
        mih={22}
        px={4}
        aria-label={expanded ? "Hide tool details" : "Show tool details"}
        aria-expanded={expanded}
        data-tool-run-toggle
        onClick={() => disclosures.toggleTool(item.id)}
        style={{ display: "flex", alignItems: "center", gap: 4, minWidth: 0 }}
      >
        <Text component="span" size="xs" fw={600} c="blue" aria-hidden="true">
          {expanded ? "−" : "+"}
        </Text>
        <Badge component="span" size="xs" variant="light" color="cyan" style={{ flexShrink: 0 }}>
          {item.tools.length}
        </Badge>
        <Text
          component="span"
          size="xs"
          c="dimmed"
          lineClamp={1}
          style={{ minWidth: 0, flex: 1, overflowWrap: "anywhere" }}
        >
          {toolRunPreview(item)}
        </Text>
        {item.status !== "complete" && (
          <Badge
            component="span"
            size="xs"
            variant="dot"
            color={toolStatusColor(item.status)}
            style={{ flexShrink: 0 }}
          >
            {item.status}
          </Badge>
        )}
      </UnstyledButton>
      {expanded && (
        <Stack gap="sm" mt="xs">
          <ToolRun item={item} />
        </Stack>
      )}
    </Paper>
  );
}

function ToolActivityGroup({ item, session }: { item: ToolGroup; session: SessionSummary }): JSX.Element {
  const disclosures = useToolDisclosures();
  const runIds = item.items.filter((row) => row.kind === "tool-run").map((row) => row.id);
  const expanded = runIds.some((id) => disclosures.groups.has(id) || disclosures.tools.has(id));
  const tools = item.items.flatMap((row) => (row.kind === "tool-run" ? row.tools : []));
  const statuses = [...new Set(tools.map((tool) => tool.status))].filter((status) => status !== "complete");
  return (
    <Box
      data-fold-kind="tool-group"
      data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
      data-tool-count={tools.length}
    >
      <Box py={2}>
        <UnstyledButton
          w="100%"
          mih={22}
          px={4}
          aria-label={expanded ? "Hide activity group" : "Show activity group"}
          aria-expanded={expanded}
          data-tool-group-toggle
          onClick={() => disclosures.toggleGroup(runIds, expanded)}
          style={{ display: "flex", alignItems: "center", gap: 4, minWidth: 0 }}
          title={toolGroupSummary(item)}
        >
          <Text component="span" size="xs" aria-hidden="true">
            {expanded ? "−" : "+"}
          </Text>
          <Text component="span" size="xs" truncate style={{ flex: 1, minWidth: 0 }}>
            {toolGroupSummary(item)}
          </Text>
          {statuses.map((status) => (
            <Badge
              component="span"
              key={status}
              size="xs"
              variant="dot"
              color={toolStatusColor(status)}
              style={{ flexShrink: 0 }}
            >
              {status}
            </Badge>
          ))}
        </UnstyledButton>
      </Box>
      {expanded && (
        <Stack gap={4} pl="xs" style={{ borderLeft: "1px solid var(--mantine-color-default-border)" }}>
          {item.items.map((row) => (
            <TranscriptCard key={row.id} item={row} session={session} />
          ))}
        </Stack>
      )}
    </Box>
  );
}

function TranscriptCard({ item, session }: { item: TranscriptItem; session: SessionSummary }): JSX.Element {
  if (item.kind === "tool-run") return <CompactToolRun item={item} />;
  if (item.kind === "narration") {
    return (
      <Text
        component="p"
        size="sm"
        my={4}
        data-fold-kind="narration"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
      >
        {item.text}
      </Text>
    );
  }
  const time = transcriptEventTime(item);
  if (item.kind === "message") {
    return (
      <Paper
        component="article"
        aria-label={item.role === "user" ? "You" : "Claude"}
        title={time ?? undefined}
        data-fold-kind="message"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        data-message-role={item.role}
        data-parent-tool-use-id={item.parentToolUseId}
        radius="sm"
        bg={item.role === "user" ? "var(--mantine-color-default-hover)" : undefined}
        p={item.role === "user" ? "xs" : 0}
        my={4}
      >
        {item.parentToolUseId !== undefined && (
          <Badge size="xs" variant="light">
            Subagent
          </Badge>
        )}
        <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {item.text}
        </Text>
      </Paper>
    );
  }
  if (item.kind === "activity" && (item.status === "complete" || item.status === "completed")) {
    return (
      <Box
        component="details"
        data-fold-kind="activity"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        title={time ?? undefined}
        py={2}
      >
        <Box
          component="summary"
          fz="xs"
          c="dimmed"
          data-activity-summary
          title={item.title}
          style={{ cursor: "pointer", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
        >
          {item.title} · completed
        </Box>
        <Text size="sm" py="xs" data-activity-title style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {item.title}
        </Text>
        {item.detail !== undefined && (
          <Text size="sm" py="xs" data-activity-detail style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {item.detail}
          </Text>
        )}
      </Box>
    );
  }
  if (
    item.kind === "summary" &&
    item.events.every((event) => event.payload.subtype === "success" && event.payload.is_error !== true)
  ) {
    return (
      <Box
        component="details"
        data-fold-kind="summary"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        title={time ?? undefined}
        py={2}
      >
        <Box component="summary" fz="xs" c="dimmed" style={{ cursor: "pointer" }}>
          {item.title}
        </Box>
        <Stack gap={4} py="xs">
          {item.details.map((detail, index) => (
            <Text key={index} size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {detail}
            </Text>
          ))}
        </Stack>
      </Box>
    );
  }
  if (item.kind === "thinking") {
    return (
      <Paper
        component="details"
        aria-label="Thinking"
        data-fold-kind="thinking"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        radius="sm"
        py={2}
        title={time ?? undefined}
      >
        <Box component="summary" fz="xs" c="dimmed" style={{ cursor: "pointer" }}>
          <Text component="span" size="xs" fw={500}>
            Thinking
          </Text>
        </Box>
        <Text size="sm" mt="xs" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {item.text}
        </Text>
      </Paper>
    );
  }
  const title =
    item.kind === "peer-message"
      ? `Message from ${item.name ?? item.from}`
      : item.kind === "peer-hold"
        ? item.state === "held"
          ? "Peer message held"
          : "Peer message dropped"
        : item.kind === "activity"
          ? item.title
          : item.kind === "context"
            ? "Context usage"
            : item.kind === "stats"
              ? "Code statistics"
              : item.kind === "usage"
                ? "Plan usage"
                : item.kind === "status"
                  ? "Session status"
                  : item.kind === "summary" || item.kind === "notice"
                    ? item.title
                    : "Transcript item";
  const color =
    item.kind === "peer-message"
      ? "blue"
      : item.kind === "peer-hold"
        ? item.state === "held"
          ? "yellow"
          : "gray"
        : "gray";
  return (
    <Paper
      component="article"
      aria-label={title}
      data-fold-kind={item.kind}
      data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
      data-peer-from={item.kind === "peer-message" || item.kind === "peer-hold" ? item.from : undefined}
      data-peer-handback={item.kind === "peer-message" && item.handback ? "true" : undefined}
      data-peer-state={item.kind === "peer-hold" ? item.state : undefined}
      data-context-model={item.kind === "context" ? item.model : undefined}
      data-stats-state={item.kind === "stats" ? (item.stats === null ? "loading" : "data") : undefined}
      withBorder
      radius="sm"
      p="md"
    >
      <Stack gap="sm">
        <Group justify="space-between" align="center" gap="xs">
          <Group gap="xs">
            <Badge variant="light" color={color}>
              {title}
            </Badge>
            {item.kind === "activity" && (
              <Badge variant="dot" color={toolStatusColor(item.status)}>
                {item.status}
              </Badge>
            )}
            {item.kind === "peer-message" && item.handback && (
              <Badge size="xs" variant="light" color="yellow">
                Subagent hand-back
              </Badge>
            )}
          </Group>
          {time !== null && (
            <Text component="time" size="xs" c="dimmed" dateTime={item.events.at(-1)?.created_at}>
              {time}
            </Text>
          )}
        </Group>
        {item.kind === "peer-message" && (
          <Stack gap="xs">
            {item.handbackNote !== undefined && (
              <Alert variant="light" color="yellow" title="Hand-back note">
                <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {item.handbackNote}
                </Text>
              </Alert>
            )}
            <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {item.text}
            </Text>
          </Stack>
        )}
        {item.kind === "peer-hold" && (
          <Stack gap="xs">
            <Text size="sm" c="dimmed">
              From {item.name ?? item.from}
            </Text>
            <Group gap="xs">
              <Badge variant="light" color={item.state === "held" ? "yellow" : "gray"}>
                {item.state === "held" ? "Held" : "Dropped"}
              </Badge>
              {item.cause !== undefined && <Badge variant="light">{item.cause.replaceAll("-", " ")}</Badge>}
              {item.outcome !== undefined && <Badge variant="light">{item.outcome.replaceAll("-", " ")}</Badge>}
            </Group>
          </Stack>
        )}
        {item.kind === "activity" && item.detail !== undefined && (
          <FoldableDetail label="Task details" detail={item.detail} />
        )}
        {item.kind === "summary" &&
          (item.details.length === 0 ? (
            <Text size="sm">Turn complete</Text>
          ) : (
            <Group gap="xs">
              {item.details.map((detail) => (
                <Badge key={detail} variant="light" color="gray">
                  {detail}
                </Badge>
              ))}
            </Group>
          ))}
        {item.kind === "context" && (
          <Stack gap="sm">
            <Group justify="space-between" gap="xs">
              <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                {item.model}
              </Text>
              <Badge variant="light" color={item.percentage >= 90 ? "red" : item.percentage >= 70 ? "yellow" : "blue"}>
                {item.percentage}% used
              </Badge>
            </Group>
            <Progress
              aria-label="Context window used"
              value={Math.max(0, Math.min(100, item.percentage))}
              color={item.percentage >= 90 ? "red" : item.percentage >= 70 ? "yellow" : "blue"}
            />
            <Text size="sm" c="dimmed">
              {item.totalTokens.toLocaleString()} of {item.rawMaxTokens.toLocaleString()} tokens
            </Text>
            {(item.categories.length > 0 ||
              item.mcpTools.length > 0 ||
              item.memoryFiles.length > 0 ||
              item.agents.length > 0) && (
              <Accordion variant="contained" radius="sm">
                <Accordion.Item value="context-breakdown">
                  <Accordion.Control>Context breakdown</Accordion.Control>
                  <Accordion.Panel>
                    <Stack gap="xs">
                      {item.categories.map((row) => (
                        <Group key={`category-${row.name}`} justify="space-between" gap="xs">
                          <Text size="sm">{row.name}</Text>
                          <Text size="sm" c="dimmed">
                            {row.tokens.toLocaleString()} tokens
                          </Text>
                        </Group>
                      ))}
                      {item.mcpTools.map((row) => (
                        <Group key={`mcp-${row.serverName}-${row.name}`} justify="space-between" gap="xs">
                          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                            {row.name} · {row.serverName}
                          </Text>
                          <Text size="sm" c="dimmed">
                            {row.tokens.toLocaleString()} tokens
                          </Text>
                        </Group>
                      ))}
                      {item.memoryFiles.map((row) => (
                        <Group key={`memory-${row.type}-${row.path}`} justify="space-between" gap="xs">
                          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                            {row.type}: {row.path}
                          </Text>
                          <Text size="sm" c="dimmed">
                            {row.tokens.toLocaleString()} tokens
                          </Text>
                        </Group>
                      ))}
                      {item.agents.map((row) => (
                        <Group key={`agent-${row.agentType}`} justify="space-between" gap="xs">
                          <Text size="sm">{row.agentType}</Text>
                          <Text size="sm" c="dimmed">
                            {row.tokens.toLocaleString()} tokens
                          </Text>
                        </Group>
                      ))}
                    </Stack>
                  </Accordion.Panel>
                </Accordion.Item>
              </Accordion>
            )}
          </Stack>
        )}
        {item.kind === "stats" &&
          (item.stats === null ? (
            <Group gap="xs" role="status">
              <Loader size="xs" aria-label="Loading code statistics" />
              <Text size="sm" c="dimmed">
                Loading code statistics…
              </Text>
            </Group>
          ) : (
            (() => {
              const activity = Array.isArray(item.stats.dailyActivity)
                ? item.stats.dailyActivity
                    .map(inputRecord)
                    .filter((value): value is Record<string, unknown> => value !== null)
                : [];
              const total = (field: string): number =>
                activity.reduce((sum, row) => sum + (typeof row[field] === "number" ? (row[field] as number) : 0), 0);
              return (
                <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="xs">
                  {[
                    ["Active days", activity.length],
                    ["Sessions", total("sessionCount")],
                    ["Messages", total("messageCount")],
                    ["Tool calls", total("toolCallCount")],
                  ].map(([label, value]) => (
                    <Paper key={label} withBorder radius="xs" p="xs">
                      <Text size="xs" c="dimmed">
                        {label}
                      </Text>
                      <Text fw={600}>{value.toLocaleString()}</Text>
                    </Paper>
                  ))}
                </SimpleGrid>
              );
            })()
          ))}
        {item.kind === "usage" && (
          <Text size="sm" c="dimmed">
            Claude Code reported plan usage for this session.
          </Text>
        )}
        {item.kind === "status" && (
          <Stack gap="xs">
            <Group gap="xs">
              <Text size="sm" c="dimmed">
                Synced session state
              </Text>
              <Badge variant="light" color={statusColor(session.status)}>
                {session.status}
              </Badge>
            </Group>
            <Text size="sm" style={{ overflowWrap: "anywhere" }}>
              {sessionSubtitle(session)}
            </Text>
          </Stack>
        )}
        {item.kind === "notice" && item.detail !== undefined && <FoldableDetail label="Details" detail={item.detail} />}
      </Stack>
    </Paper>
  );
}

export function SessionViewer(): JSX.Element {
  const isMobile = useMediaQuery("(max-width: 48em)", false);
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [search, setSearch] = useState("");
  const [showRawEvents, setShowRawEvents] = useState(false);
  const [sidebarVisible, setSidebarVisible] = useState(readSidebarVisibility);
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(readSidebarWidth);
  const [layoutWidth, setLayoutWidth] = useState(0);
  const [isResizing, setIsResizing] = useState(false);
  const layoutRef = useRef<HTMLDivElement>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const [nextSessionCursor, setNextSessionCursor] = useState<string | null>(null);
  const [resumeToken, setResumeToken] = useState<string | null>(null);
  const [watchStatus, setWatchStatus] = useState<WatchStatus>("connecting");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const filterRef = useRef(filter);
  filterRef.current = filter;
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const sessionsFilter = useRef<StatusFilter | null>(null);
  const loadedSession = useRef<string | null>(null);
  const loadedEventsSession = useRef<string | null>(null);
  const eventHistory = useRef<SessionEvent[]>([]);
  const eventViewport = useRef<HTMLDivElement | null>(null);
  const followTail = useRef(true);
  const pendingTranscriptScroll = useRef<TranscriptScrollIntent | null>(null);
  const [nextEventCursor, setNextEventCursor] = useState<string | null>(null);
  const [hasMoreEvents, setHasMoreEvents] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [eventError, setEventError] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [loadingMoreSessions, setLoadingMoreSessions] = useState(false);
  const [loadingMoreEvents, setLoadingMoreEvents] = useState(false);
  const [sessionReload, setSessionReload] = useState(0);
  const sessionVersions = useRef(new Map<string, number>());
  const appliedDetailVersions = useRef(new Map<string, number>());
  const detailRequests = useRef(new Map<string, AbortController>());
  const olderSessionsRequest = useRef<AbortController | null>(null);
  const pendingEventRefresh = useRef<string | null>(null);
  const olderPageRequest = useRef<AbortController | null>(null);
  const eventCatchupJob = useRef<{
    sessionId: string;
    controller: AbortController;
    running: boolean;
    dirty: boolean;
  } | null>(null);
  const requestEventCatchupRef = useRef<(sessionId: string) => void>(() => {});

  const replaceSessions = useCallback((next: SessionSummary[]): void => {
    sessionsRef.current = next;
    setSessions(next);
  }, []);

  const applySessionDetail = useCallback(
    (sessionId: string, summary: SessionSummary | null, appliedVersion: number): void => {
      const previous = sessionsRef.current;
      const retained = previous.filter((candidate) => candidate.id !== sessionId);
      appliedDetailVersions.current.set(sessionId, appliedVersion);
      const next = sortSessions(
        summary !== null && sessionMatchesFilter(summary, filterRef.current) ? [summary, ...retained] : retained
      );
      replaceSessions(next);
      if (selectedIdRef.current === sessionId && !next.some((candidate) => candidate.id === sessionId)) {
        setSelectedId(next[0]?.id ?? null);
      }
    },
    [replaceSessions]
  );

  const loadSessionDetail = useCallback(
    async (sessionId: string, controller: AbortController): Promise<void> => {
      while (!controller.signal.aborted) {
        const requestedVersion = sessionVersions.current.get(sessionId) ?? 0;
        try {
          const response = await retryTransient(() => getSession(sessionId, controller.signal), controller.signal);
          if (controller.signal.aborted) return;
          applySessionDetail(sessionId, response.session, requestedVersion);
          if (requestedVersion !== (sessionVersions.current.get(sessionId) ?? 0)) continue;
          return;
        } catch (reason) {
          if (controller.signal.aborted) return;
          if (requestedVersion !== (sessionVersions.current.get(sessionId) ?? 0)) continue;
          if (reason instanceof ApiError && reason.status === 404) {
            applySessionDetail(sessionId, null, requestedVersion);
          } else {
            setSessionError(errorMessage(reason));
          }
          return;
        }
      }
    },
    [applySessionDetail]
  );

  const refreshSessionDetail = useCallback(
    (sessionId: string): void => {
      sessionVersions.current.set(sessionId, (sessionVersions.current.get(sessionId) ?? 0) + 1);
      if (detailRequests.current.has(sessionId)) return;
      const controller = new AbortController();
      detailRequests.current.set(sessionId, controller);
      void loadSessionDetail(sessionId, controller).finally(() => {
        if (detailRequests.current.get(sessionId) === controller) detailRequests.current.delete(sessionId);
      });
    },
    [loadSessionDetail]
  );

  const refreshSelectedEvents = useCallback((sessionId: string | null): void => {
    if (sessionId === null) return;
    if (loadedEventsSession.current === sessionId) {
      requestEventCatchupRef.current(sessionId);
    } else {
      pendingEventRefresh.current = sessionId;
    }
  }, []);

  const handleChangedSessions = useCallback(
    (sessionIds: string[]): void => {
      const changedIds = [...new Set(sessionIds)];
      for (const sessionId of changedIds) refreshSessionDetail(sessionId);
      const selected = selectedIdRef.current;
      if (selected !== null && changedIds.includes(selected)) refreshSelectedEvents(selected);
    },
    [refreshSelectedEvents, refreshSessionDetail]
  );

  const handleWatchReset = useCallback((): void => {
    for (const session of sessionsRef.current) refreshSessionDetail(session.id);
    setSessionReload((count) => count + 1);
    refreshSelectedEvents(selectedIdRef.current);
  }, [refreshSelectedEvents, refreshSessionDetail]);

  const changedHandlerRef = useRef(handleChangedSessions);
  changedHandlerRef.current = handleChangedSessions;
  const resetHandlerRef = useRef(handleWatchReset);
  resetHandlerRef.current = handleWatchReset;

  const captureScrollAnchor = useCallback((sessionId: string): void => {
    const viewport = eventViewport.current;
    if (viewport === null) return;
    const viewportTop = viewport.getBoundingClientRect().top;
    const candidates = [...viewport.querySelectorAll<HTMLElement>("[data-fold-kind], [data-raw-event]")];
    const element = candidates.find((candidate) => candidate.getBoundingClientRect().bottom > viewportTop) ?? null;
    const sequences =
      element?.dataset.historySequences?.split(" ") ??
      (element?.dataset.sequence === undefined ? [] : [element.dataset.sequence]);
    pendingTranscriptScroll.current = {
      kind: "anchor",
      sessionId,
      element,
      sequences,
      top: element?.getBoundingClientRect().top ?? viewportTop,
      scrollTop: viewport.scrollTop,
      scrollHeight: viewport.scrollHeight,
    };
  }, []);

  const requestEventCatchup = useCallback(
    (sessionId: string): void => {
      if (loadedEventsSession.current !== sessionId || loadedSession.current !== sessionId) {
        pendingEventRefresh.current = sessionId;
        return;
      }
      let job = eventCatchupJob.current;
      if (job !== null && job.sessionId !== sessionId) {
        job.controller.abort();
        eventCatchupJob.current = null;
        job = null;
      }
      if (job === null) {
        job = { sessionId, controller: new AbortController(), running: false, dirty: false };
        eventCatchupJob.current = job;
      }
      job.dirty = true;
      if (job.running) return;
      job.running = true;
      const activeJob = job;
      void (async () => {
        while (
          activeJob.dirty &&
          !activeJob.controller.signal.aborted &&
          loadedEventsSession.current === sessionId &&
          loadedSession.current === sessionId
        ) {
          activeJob.dirty = false;
          try {
            const newestPage = await retryTransient(
              () => listSessionEvents(sessionId, undefined, "desc", activeJob.controller.signal),
              activeJob.controller.signal
            );
            if (
              activeJob.controller.signal.aborted ||
              loadedSession.current !== sessionId ||
              loadedEventsSession.current !== sessionId
            )
              return;
            const previousNewest = eventHistory.current.at(-1);
            const catchUpEvents: SessionEvent[] = [];
            if (previousNewest !== undefined) {
              const frontier = newestPage.data.reduce(
                (latest, event) => (BigInt(event.sequence_num) > latest ? BigInt(event.sequence_num) : latest),
                BigInt(previousNewest.sequence_num)
              );
              let cursor = previousNewest.event_id;
              while (true) {
                const page = await retryTransient(
                  () => listSessionEvents(sessionId, cursor, "asc", activeJob.controller.signal),
                  activeJob.controller.signal
                );
                if (
                  activeJob.controller.signal.aborted ||
                  loadedSession.current !== sessionId ||
                  loadedEventsSession.current !== sessionId
                )
                  return;
                const previousCursor = cursor;
                if (page.data.length > 0) {
                  const throughFrontier = page.data.filter((event) => BigInt(event.sequence_num) <= frontier);
                  catchUpEvents.push(...throughFrontier);
                  if (page.last_id === null) throw new Error("The event page omitted its cursor.");
                  cursor = page.last_id;
                  if (page.data.some((event) => BigInt(event.sequence_num) > frontier)) break;
                  if (throughFrontier.some((event) => BigInt(event.sequence_num) === frontier)) break;
                }
                if (!page.has_more) break;
                if (page.data.length === 0 || page.last_id === null || page.last_id === previousCursor) {
                  throw new Error("The event history cursor did not advance.");
                }
              }
            }
            if (activeJob.controller.signal.aborted || loadedSession.current !== sessionId) return;
            const merged = mergeSessionEvents(eventHistory.current, [...newestPage.data, ...catchUpEvents]);
            if (merged !== eventHistory.current) {
              if (followTail.current) {
                pendingTranscriptScroll.current = { kind: "tail", sessionId };
              } else {
                captureScrollAnchor(sessionId);
              }
              eventHistory.current = merged;
              setEvents(merged);
            }
            if (previousNewest === undefined) {
              setNextEventCursor(newestPage.has_more ? newestPage.last_id : null);
              setHasMoreEvents(newestPage.has_more);
            }
            setEventError(null);
          } catch (reason) {
            if (!activeJob.controller.signal.aborted && loadedSession.current === sessionId) {
              setEventError(errorMessage(reason));
            }
            activeJob.dirty = false;
          }
        }
      })().finally(() => {
        activeJob.running = false;
        if (eventCatchupJob.current === activeJob && activeJob.controller.signal.aborted) {
          eventCatchupJob.current = null;
        } else if (eventCatchupJob.current === activeJob && activeJob.dirty) {
          requestEventCatchupRef.current(sessionId);
        }
      });
    },
    [captureScrollAnchor]
  );
  requestEventCatchupRef.current = requestEventCatchup;

  useEffect(() => {
    const job = eventCatchupJob.current;
    if (job !== null && job.sessionId !== selectedId) {
      job.controller.abort();
      eventCatchupJob.current = null;
    }
  }, [selectedId]);

  const updateTail = useCallback((): void => {
    const viewport = eventViewport.current;
    if (viewport !== null) {
      followTail.current = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 48;
    }
  }, []);

  const setEventViewport = useCallback(
    (viewport: HTMLDivElement | null): void => {
      const previous = eventViewport.current;
      if (previous !== null) previous.removeEventListener("scroll", updateTail);
      eventViewport.current = viewport;
      if (viewport === null) return;
      viewport.addEventListener("scroll", updateTail, { passive: true });
      updateTail();
    },
    [updateTail]
  );

  useLayoutEffect(() => {
    const viewport = eventViewport.current;
    const intent = pendingTranscriptScroll.current;
    if (viewport === null || intent === null || intent.sessionId !== selectedId) return;
    if (intent.kind === "tail") {
      viewport.scrollTop = viewport.scrollHeight;
      followTail.current = true;
      pendingTranscriptScroll.current = null;
      return;
    }

    let anchor = intent.element?.isConnected === true ? intent.element : null;
    if (anchor === null && intent.sequences.length > 0) {
      anchor =
        [...viewport.querySelectorAll<HTMLElement>("[data-history-sequences], [data-sequence]")].find((candidate) => {
          const sequences =
            candidate.dataset.historySequences?.split(" ") ??
            (candidate.dataset.sequence === undefined ? [] : [candidate.dataset.sequence]);
          return intent.sequences.some((sequence) => sequences.includes(sequence));
        }) ?? null;
    }
    if (anchor !== null) {
      viewport.scrollTop += anchor.getBoundingClientRect().top - intent.top;
    } else {
      viewport.scrollTop = intent.scrollTop + (viewport.scrollHeight - intent.scrollHeight);
    }
    followTail.current = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 48;
    pendingTranscriptScroll.current = null;
  }, [events, selectedId]);

  const availableLayoutWidth = layoutWidth || Math.max(0, window.innerWidth - 64);
  const sidebarMaxWidth = maxSidebarWidth(availableLayoutWidth);
  const visibleSidebarWidth = Math.min(sidebarWidth, sidebarMaxWidth);

  useEffect(() => {
    storeSidebarPreference(SIDEBAR_VISIBLE_STORAGE_KEY, String(sidebarVisible));
  }, [sidebarVisible]);

  useEffect(() => {
    storeSidebarPreference(SIDEBAR_WIDTH_STORAGE_KEY, String(sidebarWidth));
  }, [sidebarWidth]);

  useEffect(() => {
    const layout = layoutRef.current;
    if (layout === null) return;
    const updateWidth = (): void => setLayoutWidth(layout.clientWidth);
    updateWidth();
    window.addEventListener("resize", updateWidth);
    if (typeof ResizeObserver === "undefined") {
      return () => window.removeEventListener("resize", updateWidth);
    }
    const observer = new ResizeObserver(updateWidth);
    observer.observe(layout);
    return () => {
      window.removeEventListener("resize", updateWidth);
      observer.disconnect();
    };
  }, [isMobile, sidebarVisible]);

  useEffect(() => {
    if (!isResizing) return;
    const resize = (event: PointerEvent): void => {
      const bounds = layoutRef.current?.getBoundingClientRect();
      if (bounds !== undefined) {
        setSidebarWidth(clampSidebarWidth(Math.min(event.clientX - bounds.left, maxSidebarWidth(bounds.width))));
      }
    };
    const stopResizing = (): void => setIsResizing(false);
    window.addEventListener("pointermove", resize);
    window.addEventListener("pointerup", stopResizing);
    window.addEventListener("pointercancel", stopResizing);
    return () => {
      window.removeEventListener("pointermove", resize);
      window.removeEventListener("pointerup", stopResizing);
      window.removeEventListener("pointercancel", stopResizing);
    };
  }, [isResizing]);

  useEffect(() => {
    let current = true;
    const controller = new AbortController();
    const filterChanged = sessionsFilter.current !== filter;
    const statuses = filter === "all" ? ALL_STATUSES : [filter];
    const versionsAtStart = new Map(sessionVersions.current);
    const appliedVersionsAtStart = new Map(appliedDetailVersions.current);
    olderSessionsRequest.current?.abort();
    olderSessionsRequest.current = null;
    setLoadingMoreSessions(false);
    setSessionError(null);
    void retryTransient(() => listSessions(statuses, undefined, controller.signal), controller.signal)
      .then((page) => {
        if (!current || controller.signal.aborted) return;
        sessionsFilter.current = filter;
        const previous = sessionsRef.current.filter((session) => sessionMatchesFilter(session, filter));
        const previousById = new Map(previous.map((session) => [session.id, session]));
        const currentPage = page.data.flatMap((session) => {
          const changedSinceRead =
            (sessionVersions.current.get(session.id) ?? 0) > (versionsAtStart.get(session.id) ?? 0);
          const resolved = changedSinceRead ? previousById.get(session.id) : session;
          return resolved !== undefined && sessionMatchesFilter(resolved, filter) ? [resolved] : [];
        });
        const resolvedDuringRequest = sessionsRef.current.filter(
          (session) =>
            sessionMatchesFilter(session, filter) &&
            (appliedDetailVersions.current.get(session.id) ?? 0) > (appliedVersionsAtStart.get(session.id) ?? 0)
        );
        const next = sortSessions(
          mergeSessionPage(resolvedDuringRequest, filterChanged ? currentPage : mergeSessionPage(currentPage, previous))
        );
        replaceSessions(next);
        setNextSessionCursor(page.next_cursor);
        setResumeToken((existing) => existing ?? page.resume_token ?? null);
        setSelectedId((previous) =>
          filterChanged && !next.some((session) => session.id === previous)
            ? (next[0]?.id ?? null)
            : (previous ?? next[0]?.id ?? null)
        );
        setSessionError(null);
      })
      .catch((reason: unknown) => {
        if (current && !controller.signal.aborted) setSessionError(errorMessage(reason));
      })
      .finally(() => {
        if (current && !controller.signal.aborted) setLoadingSessions(false);
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [filter, sessionReload, replaceSessions]);

  useEffect(() => {
    if (resumeToken === null) return;
    const watch = watchSessions(resumeToken);
    const recoverMalformedFrame = (): void => {
      setSessionError("The live update stream sent an invalid change. Reloading the loaded session list.");
      resetHandlerRef.current();
    };
    const changed = (event: Event): void => {
      if (!(event instanceof MessageEvent)) {
        recoverMalformedFrame();
        return;
      }
      try {
        const payload: unknown = JSON.parse(event.data);
        if (
          typeof payload !== "object" ||
          payload === null ||
          !Array.isArray((payload as { session_ids?: unknown }).session_ids)
        ) {
          recoverMalformedFrame();
          return;
        }
        const ids = (payload as { session_ids: unknown[] }).session_ids.filter(
          (sessionId): sessionId is string => typeof sessionId === "string" && sessionId.length > 0
        );
        if (ids.length !== (payload as { session_ids: unknown[] }).session_ids.length) {
          recoverMalformedFrame();
          return;
        }
        changedHandlerRef.current(ids);
      } catch {
        recoverMalformedFrame();
      }
    };
    const reset = (): void => resetHandlerRef.current();
    setWatchStatus("connecting");
    watch.onopen = () => setWatchStatus("connected");
    watch.onerror = () => setWatchStatus("reconnecting");
    watch.addEventListener("changed", changed);
    watch.addEventListener("reset", reset);
    return () => {
      watch.removeEventListener("changed", changed);
      watch.removeEventListener("reset", reset);
      watch.close();
    };
  }, [resumeToken]);

  useEffect(
    () => () => {
      olderSessionsRequest.current?.abort();
      eventCatchupJob.current?.controller.abort();
      eventCatchupJob.current = null;
      for (const controller of detailRequests.current.values()) controller.abort();
      detailRequests.current.clear();
    },
    []
  );

  useEffect(() => {
    let current = true;
    if (selectedId === null) {
      loadedSession.current = null;
      loadedEventsSession.current = null;
      eventHistory.current = [];
      pendingTranscriptScroll.current = null;
      pendingEventRefresh.current = null;
      olderPageRequest.current?.abort();
      olderPageRequest.current = null;
      setLoadingEvents(false);
      setLoadingMoreEvents(false);
      setEvents([]);
      setNextEventCursor(null);
      setHasMoreEvents(false);
      return () => {
        current = false;
      };
    }
    loadedSession.current = selectedId;
    loadedEventsSession.current = null;
    eventHistory.current = [];
    pendingTranscriptScroll.current = null;
    if (pendingEventRefresh.current !== selectedId) pendingEventRefresh.current = null;
    olderPageRequest.current?.abort();
    olderPageRequest.current = null;
    followTail.current = true;
    setLoadingEvents(true);
    setLoadingMoreEvents(false);
    setEvents([]);
    setNextEventCursor(null);
    setHasMoreEvents(false);
    setEventError(null);
    const controller = new AbortController();
    void retryTransient(() => listSessionEvents(selectedId, undefined, "desc", controller.signal), controller.signal)
      .then((page) => {
        if (!current || loadedSession.current !== selectedId) return;
        const merged = mergeSessionEvents([], page.data);
        eventHistory.current = merged;
        loadedEventsSession.current = selectedId;
        if (merged.length > 0) {
          pendingTranscriptScroll.current = { kind: "tail", sessionId: selectedId };
          setEvents(merged);
        }
        setNextEventCursor(page.has_more ? page.last_id : null);
        setHasMoreEvents(page.has_more);
        if (pendingEventRefresh.current === selectedId) {
          pendingEventRefresh.current = null;
          requestEventCatchupRef.current(selectedId);
        }
      })
      .catch((reason: unknown) => {
        if (current && !controller.signal.aborted) setEventError(errorMessage(reason));
      })
      .finally(() => {
        if (current) setLoadingEvents(false);
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [selectedId]);

  const visibleSessions = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const matchingStatus = sessions.filter((session) => sessionMatchesFilter(session, filter));
    return needle === ""
      ? matchingStatus
      : matchingStatus.filter((session) => JSON.stringify(session).toLowerCase().includes(needle));
  }, [filter, search, sessions]);

  const selectedSession =
    sessions.find((session) => session.id === selectedId && sessionMatchesFilter(session, filter)) ?? null;
  const transcript = useMemo(() => foldSessionEvents(events), [events]);
  const rows = useMemo(() => groupToolActivity(transcript), [transcript]);
  const watchLabel =
    watchStatus === "connected" ? "Live updates on" : watchStatus === "connecting" ? "Connecting…" : "Reconnecting…";

  const loadMoreSessions = useCallback(async (): Promise<void> => {
    if (nextSessionCursor === null || loadingMoreSessions) return;
    const controller = new AbortController();
    olderSessionsRequest.current?.abort();
    olderSessionsRequest.current = controller;
    const requestedFilter = filter;
    const versionsAtStart = new Map(sessionVersions.current);
    setLoadingMoreSessions(true);
    setSessionError(null);
    try {
      const page = await retryTransient(
        () =>
          listSessions(
            requestedFilter === "all" ? ALL_STATUSES : [requestedFilter],
            nextSessionCursor,
            controller.signal
          ),
        controller.signal
      );
      if (controller.signal.aborted || filterRef.current !== requestedFilter) return;
      const previous = sessionsRef.current.filter((session) => sessionMatchesFilter(session, requestedFilter));
      const previousById = new Map(previous.map((session) => [session.id, session]));
      const currentPage = page.data.flatMap((session) => {
        const changedSinceRead =
          (sessionVersions.current.get(session.id) ?? 0) > (versionsAtStart.get(session.id) ?? 0);
        const resolved = changedSinceRead ? previousById.get(session.id) : session;
        return resolved !== undefined && sessionMatchesFilter(resolved, requestedFilter) ? [resolved] : [];
      });
      replaceSessions(sortSessions(mergeSessionPage(currentPage, previous)));
      setNextSessionCursor(page.next_cursor);
    } catch (reason) {
      if (!controller.signal.aborted) setSessionError(errorMessage(reason));
    } finally {
      if (olderSessionsRequest.current === controller) {
        olderSessionsRequest.current = null;
        setLoadingMoreSessions(false);
      }
    }
  }, [filter, loadingMoreSessions, nextSessionCursor, replaceSessions]);

  const loadMoreEvents = useCallback(async (): Promise<void> => {
    const sessionId = selectedId;
    if (sessionId === null || nextEventCursor === null || loadingMoreEvents) return;
    const controller = new AbortController();
    olderPageRequest.current?.abort();
    olderPageRequest.current = controller;
    setLoadingMoreEvents(true);
    setEventError(null);
    try {
      const page = await listSessionEvents(sessionId, nextEventCursor, "desc", controller.signal);
      if (controller.signal.aborted || loadedSession.current !== sessionId) return;
      const merged = mergeSessionEvents(eventHistory.current, page.data);
      if (merged !== eventHistory.current) {
        captureScrollAnchor(sessionId);
        eventHistory.current = merged;
        setEvents(merged);
      }
      setNextEventCursor(page.has_more ? page.last_id : null);
      setHasMoreEvents(page.has_more);
    } catch (reason) {
      if (!controller.signal.aborted && loadedSession.current === sessionId) setEventError(errorMessage(reason));
    } finally {
      if (olderPageRequest.current === controller) {
        olderPageRequest.current = null;
        if (loadedSession.current === sessionId) setLoadingMoreEvents(false);
      }
    }
  }, [captureScrollAnchor, loadingMoreEvents, nextEventCursor, selectedId]);

  const toggleSidebar = (): void => {
    if (isMobile) {
      setMobileDrawerOpen((opened) => !opened);
    } else {
      setSidebarVisible((visible) => !visible);
    }
  };

  const renderSessionList = (id: string, closeDrawerOnSelect: boolean): JSX.Element => (
    <SessionList
      id={id}
      sessions={sessions}
      visibleSessions={visibleSessions}
      selectedId={selectedId}
      loadingSessions={loadingSessions}
      nextSessionCursor={nextSessionCursor}
      loadingMoreSessions={loadingMoreSessions}
      search={search}
      filter={filter}
      onSearchChange={setSearch}
      onFilterChange={setFilter}
      onSelect={(sessionId) => {
        setSelectedId(sessionId);
        if (closeDrawerOnSelect) setMobileDrawerOpen(false);
      }}
      onLoadMore={() => void loadMoreSessions()}
    />
  );

  return (
    <>
      <Paper
        component="div"
        role="region"
        aria-label="Session history"
        withBorder
        radius="md"
        p="sm"
        style={{ display: "flex", flex: 1, minHeight: 0, minWidth: 0, overflow: "hidden" }}
      >
        <Stack gap="sm" style={{ flex: 1, minHeight: 0, minWidth: 0 }}>
          <Group justify="flex-end" align="center" wrap="wrap">
            <Group gap="sm">
              {resumeToken !== null && (
                <Badge role="status" variant="dot" color={watchStatus === "connected" ? "green" : "yellow"}>
                  {watchLabel}
                </Badge>
              )}
              <Button
                variant="default"
                size="compact-xs"
                aria-controls={isMobile ? "session-sidebar-mobile" : "session-sidebar"}
                aria-expanded={isMobile ? mobileDrawerOpen : sidebarVisible}
                onClick={toggleSidebar}
              >
                {isMobile ? "Session list" : sidebarVisible ? "Hide session list" : "Show session list"}
              </Button>
            </Group>
          </Group>

          {sessionError !== null && (
            <Alert color="red" title="Could not load sessions">
              {sessionError}
            </Alert>
          )}

          <div
            ref={layoutRef}
            data-session-viewer-layout
            style={{
              display: "flex",
              flex: 1,
              minHeight: 0,
              minWidth: 0,
              overflow: "hidden",
              userSelect: isResizing ? "none" : undefined,
            }}
          >
            {!isMobile && sidebarVisible && (
              <div
                data-session-sidebar-width={visibleSidebarWidth}
                style={{ flex: `0 0 ${visibleSidebarWidth}px`, minWidth: 0, overflow: "hidden" }}
              >
                {renderSessionList("session-sidebar", false)}
              </div>
            )}
            {!isMobile && sidebarVisible && (
              <div
                role="separator"
                aria-label="Resize session list"
                aria-controls="session-sidebar"
                aria-orientation="vertical"
                aria-valuemin={SIDEBAR_MIN_WIDTH}
                aria-valuemax={sidebarMaxWidth}
                aria-valuenow={visibleSidebarWidth}
                aria-valuetext={`${visibleSidebarWidth} pixels`}
                tabIndex={0}
                data-session-sidebar-resizer
                onPointerDown={(event) => {
                  if (event.button !== 0 || event.isPrimary === false) return;
                  event.preventDefault();
                  setIsResizing(true);
                }}
                onKeyDown={(event) => {
                  if (event.key === "ArrowLeft") {
                    event.preventDefault();
                    setSidebarWidth(clampSidebarWidth(visibleSidebarWidth - 16));
                  } else if (event.key === "ArrowRight") {
                    event.preventDefault();
                    setSidebarWidth(clampSidebarWidth(Math.min(visibleSidebarWidth + 16, sidebarMaxWidth)));
                  } else if (event.key === "Home") {
                    event.preventDefault();
                    setSidebarWidth(SIDEBAR_MIN_WIDTH);
                  } else if (event.key === "End") {
                    event.preventDefault();
                    setSidebarWidth(sidebarMaxWidth);
                  }
                }}
                style={{
                  alignSelf: "stretch",
                  cursor: "col-resize",
                  flex: `0 0 ${SIDEBAR_RESIZER_WIDTH}px`,
                  outlineOffset: -2,
                  touchAction: "none",
                  userSelect: isResizing ? "none" : undefined,
                }}
              >
                <div
                  aria-hidden="true"
                  style={{
                    borderLeft: "1px solid var(--mantine-color-default-border)",
                    height: "100%",
                    margin: "auto",
                    width: 1,
                  }}
                />
              </div>
            )}

            <Paper
              component="div"
              role="region"
              aria-label="Session transcript"
              withBorder
              radius="sm"
              p="sm"
              style={{ flex: 1, height: "100%", minHeight: 0, minWidth: 0, overflow: "hidden" }}
            >
              {selectedSession === null ? (
                <Center h={240}>
                  <Text c="dimmed" ta="center">
                    Select a session to view its transcript.
                  </Text>
                </Center>
              ) : (
                <Stack gap="md" aria-busy={loadingEvents} style={{ height: "100%", minHeight: 0 }}>
                  <Group justify="space-between" align="flex-start" gap="xs">
                    <Stack gap={4}>
                      <Title order={4}>{selectedSession.title || "Untitled session"}</Title>
                      <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: "anywhere" }}>
                        {sessionSubtitle(selectedSession)}
                      </Text>
                    </Stack>
                    <Group gap={4}>
                      <Badge variant="light" color={statusColor(selectedSession.status)}>
                        {selectedSession.status}
                      </Badge>
                      <Button
                        variant={showRawEvents ? "light" : "subtle"}
                        size="compact-xs"
                        aria-label={showRawEvents ? "Show folded transcript" : "Show raw event stream"}
                        aria-pressed={showRawEvents}
                        onClick={() => setShowRawEvents((value) => !value)}
                      >
                        {showRawEvents ? "Transcript" : "Events"}
                      </Button>
                    </Group>
                  </Group>

                  {eventError !== null && (
                    <Alert color="red" title="Could not load transcript">
                      {eventError}
                    </Alert>
                  )}

                  {loadingEvents ? (
                    <Center h={180}>
                      <Loader size="sm" aria-label="Loading transcript" />
                    </Center>
                  ) : events.length === 0 ? (
                    <Center h={180}>
                      <Text c="dimmed" ta="center">
                        No events are stored for this session yet.
                      </Text>
                    </Center>
                  ) : (
                    <ScrollArea style={{ flex: 1, minHeight: 0 }} type="auto" viewportRef={setEventViewport}>
                      <Stack gap={4} pr="sm">
                        {hasMoreEvents && (
                          <Button variant="default" loading={loadingMoreEvents} onClick={() => void loadMoreEvents()}>
                            Load older events
                          </Button>
                        )}
                        {showRawEvents ? (
                          <EventInspector key={selectedId} events={events} />
                        ) : transcript.length === 0 ? (
                          <Text size="sm" c="dimmed" role="status">
                            No loaded events appear in the folded transcript.
                          </Text>
                        ) : (
                          <ToolDisclosureProvider key={selectedId}>
                            {rows.map((row) =>
                              row.kind === "tool-group" ? (
                                <ToolActivityGroup key={row.id} item={row} session={selectedSession} />
                              ) : (
                                <TranscriptCard key={`${row.kind}-${row.id}`} item={row} session={selectedSession} />
                              )
                            )}
                          </ToolDisclosureProvider>
                        )}
                      </Stack>
                    </ScrollArea>
                  )}
                </Stack>
              )}
            </Paper>
          </div>
        </Stack>
      </Paper>
      {isMobile && (
        <Drawer
          opened={mobileDrawerOpen}
          onClose={() => setMobileDrawerOpen(false)}
          title="Sessions"
          position="left"
          size="min(88vw, 24rem)"
          padding="md"
          styles={{
            body: { display: "flex", height: "calc(100dvh - 5rem)", minHeight: 0 },
            content: { height: "100dvh" },
          }}
        >
          {renderSessionList("session-sidebar-mobile", true)}
        </Drawer>
      )}
    </>
  );
}
