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
  ActionIcon,
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
} from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";

import {
  ApiError,
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
      <Group gap={4}>
        <ActionIcon
          variant={expanded ? "light" : "subtle"}
          size="xs"
          aria-label={expanded ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <Text size="xs" fw={600}>
            {expanded ? "−" : "+"}
          </Text>
        </ActionIcon>
        <Text size="xs" c="dimmed">
          {label}
        </Text>
      </Group>
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
      <Group gap={4} wrap="nowrap">
        <ActionIcon
          variant={expanded ? "light" : "subtle"}
          size="sm"
          aria-label={expanded ? "Hide tool details" : "Show tool details"}
          aria-expanded={expanded}
          data-tool-run-toggle
          onClick={() => disclosures.toggleTool(item.id)}
        >
          <Text size="xs" fw={600}>
            {expanded ? "−" : "+"}
          </Text>
        </ActionIcon>
        <Badge size="xs" variant="light" color="cyan">
          {item.tools.length}
        </Badge>
        <Text size="xs" c="dimmed" lineClamp={1} style={{ minWidth: 0, flex: 1, overflowWrap: "anywhere" }}>
          {toolRunPreview(item)}
        </Text>
        {item.status !== "complete" && (
          <Badge size="xs" variant="dot" color={toolStatusColor(item.status)}>
            {item.status}
          </Badge>
        )}
      </Group>
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
      <Group gap={4} wrap="nowrap" py={2}>
        <Button
          variant="subtle"
          color="gray"
          size="compact-xs"
          fw={400}
          aria-label={expanded ? "Hide activity group" : "Show activity group"}
          aria-expanded={expanded}
          data-tool-group-toggle
          onClick={() => disclosures.toggleGroup(runIds, expanded)}
          styles={{ root: { minWidth: 0 }, label: { display: "block", overflow: "hidden", textOverflow: "ellipsis" } }}
          title={toolGroupSummary(item)}
        >
          {expanded ? "−" : "+"} {toolGroupSummary(item)}
        </Button>
        {statuses.map((status) => (
          <Badge key={status} size="xs" variant="dot" color={toolStatusColor(status)}>
            {status}
          </Badge>
        ))}
      </Group>
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
        <Box component="summary" fz="xs" c="dimmed" style={{ cursor: "pointer" }}>
          {item.title} · completed
        </Box>
        {item.detail !== undefined && (
          <Text size="sm" py="xs" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
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
  const [nextSessionCursor, setNextSessionCursor] = useState<string | null>(null);
  const [resumeToken, setResumeToken] = useState<string | null>(null);
  const [watchStatus, setWatchStatus] = useState<WatchStatus>("connecting");
  const [selectedId, setSelectedId] = useState<string | null>(null);
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
  const [refreshCount, setRefreshCount] = useState(0);
  const [eventRefreshToken, setEventRefreshToken] = useState(0);
  const refreshCountRef = useRef(refreshCount);
  refreshCountRef.current = refreshCount;
  const eventLoadRefreshCount = useRef(0);
  const pendingEventRefresh = useRef<string | null>(null);
  const olderPageRequest = useRef<AbortController | null>(null);

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
    const filterChanged = sessionsFilter.current !== filter;
    const statuses = filter === "all" ? ALL_STATUSES : [filter];
    // Only the first fetch needs a loading placeholder; watch refreshes retain the list.
    setSessionError(null);
    void listSessions(statuses)
      .then((page) => {
        if (!current) return;
        sessionsFilter.current = filter;
        setSessions((previous) => (filterChanged ? page.data : mergeSessionPage(page.data, previous)));
        setNextSessionCursor(page.next_cursor);
        setResumeToken(page.resume_token ?? null);
        setSelectedId((previous) =>
          filterChanged && !page.data.some((session) => session.id === previous)
            ? (page.data[0]?.id ?? null)
            : (previous ?? page.data[0]?.id ?? null)
        );
      })
      .catch((reason: unknown) => {
        if (current) setSessionError(errorMessage(reason));
      })
      .finally(() => {
        if (current) setLoadingSessions(false);
      });
    return () => {
      current = false;
    };
  }, [filter, refreshCount]);

  useEffect(() => {
    if (resumeToken === null) return;
    const watch = watchSessions(resumeToken);
    const refresh = (): void => setRefreshCount((count) => count + 1);
    setWatchStatus("connecting");
    watch.onopen = () => setWatchStatus("connected");
    watch.onerror = () => setWatchStatus("reconnecting");
    watch.addEventListener("changed", refresh);
    watch.addEventListener("reset", refresh);
    return () => watch.close();
  }, [resumeToken]);

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
    pendingEventRefresh.current = null;
    olderPageRequest.current?.abort();
    olderPageRequest.current = null;
    eventLoadRefreshCount.current = refreshCountRef.current;
    followTail.current = true;
    setLoadingEvents(true);
    setLoadingMoreEvents(false);
    setEvents([]);
    setNextEventCursor(null);
    setHasMoreEvents(false);
    setEventError(null);
    const controller = new AbortController();
    void listSessionEvents(selectedId, undefined, "desc", controller.signal)
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
          setEventRefreshToken((token) => token + 1);
        }
      })
      .catch((reason: unknown) => {
        if (current) setEventError(errorMessage(reason));
      })
      .finally(() => {
        if (current) setLoadingEvents(false);
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [selectedId]);

  useEffect(() => {
    if (selectedId === null) return;
    if (loadedEventsSession.current !== selectedId) {
      if (refreshCount > eventLoadRefreshCount.current) pendingEventRefresh.current = selectedId;
      return;
    }
    let current = true;
    const controller = new AbortController();
    setEventError(null);
    void (async () => {
      try {
        const newestPage = await listSessionEvents(selectedId, undefined, "desc", controller.signal);
        if (!current || loadedSession.current !== selectedId) return;
        const previousNewest = eventHistory.current.at(-1);
        const catchUpEvents: SessionEvent[] = [];
        if (previousNewest !== undefined) {
          let cursor = previousNewest.event_id;
          while (true) {
            const page = await listSessionEvents(selectedId, cursor, "asc", controller.signal);
            if (!current || loadedSession.current !== selectedId) return;
            const previousCursor = cursor;
            if (page.data.length > 0) {
              catchUpEvents.push(...page.data);
              if (page.last_id === null) throw new Error("The event page omitted its cursor.");
              cursor = page.last_id;
            }
            if (!page.has_more) break;
            if (page.data.length === 0 || page.last_id === null || page.last_id === previousCursor) {
              throw new Error("The event history cursor did not advance.");
            }
          }
        }
        if (!current || loadedSession.current !== selectedId) return;
        const merged = mergeSessionEvents(eventHistory.current, [...newestPage.data, ...catchUpEvents]);
        if (merged !== eventHistory.current) {
          if (followTail.current) {
            pendingTranscriptScroll.current = { kind: "tail", sessionId: selectedId };
          } else {
            captureScrollAnchor(selectedId);
          }
          eventHistory.current = merged;
          setEvents(merged);
        }
        if (previousNewest === undefined) {
          setNextEventCursor(newestPage.has_more ? newestPage.last_id : null);
          setHasMoreEvents(newestPage.has_more);
        }
      } catch (reason) {
        if (current && loadedSession.current === selectedId) setEventError(errorMessage(reason));
      }
    })();
    return () => {
      current = false;
      controller.abort();
    };
  }, [captureScrollAnchor, eventRefreshToken, refreshCount, selectedId]);

  const visibleSessions = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return needle === ""
      ? sessions
      : sessions.filter((session) => JSON.stringify(session).toLowerCase().includes(needle));
  }, [search, sessions]);

  const selectedSession = sessions.find((session) => session.id === selectedId) ?? null;
  const transcript = useMemo(() => foldSessionEvents(events), [events]);
  const rows = useMemo(() => groupToolActivity(transcript), [transcript]);
  const watchLabel =
    watchStatus === "connected" ? "Live updates on" : watchStatus === "connecting" ? "Connecting…" : "Reconnecting…";

  const loadMoreSessions = useCallback(async (): Promise<void> => {
    if (nextSessionCursor === null || loadingMoreSessions) return;
    setLoadingMoreSessions(true);
    setSessionError(null);
    try {
      const page = await listSessions(filter === "all" ? ALL_STATUSES : [filter], nextSessionCursor);
      setSessions((previous) => {
        const seen = new Set(previous.map((session) => session.id));
        return [...previous, ...page.data.filter((session) => !seen.has(session.id))];
      });
      setNextSessionCursor(page.next_cursor);
    } catch (reason) {
      setSessionError(errorMessage(reason));
    } finally {
      setLoadingMoreSessions(false);
    }
  }, [filter, loadingMoreSessions, nextSessionCursor]);

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
              <Button variant="default" size="compact-xs" onClick={() => setRefreshCount((count) => count + 1)}>
                Refresh
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
