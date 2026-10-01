import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from "react";
import {
  Accordion,
  Alert,
  Badge,
  Button,
  Center,
  Code,
  Group,
  Image,
  Loader,
  NavLink,
  Paper,
  ScrollArea,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";

import {
  ApiError,
  listSessionEvents,
  listSessions,
  watchSessions,
  type SessionEvent,
  type SessionSummary,
} from "./api";
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
                    {tool.failed ? "Error output" : tool.name === "Bash" ? "Command output" : "Output"}
                  </Text>
                  {tool.result !== undefined && (
                    <Code block style={{ maxHeight: 240, overflow: "auto", whiteSpace: "pre-wrap" }}>
                      {tool.result}
                    </Code>
                  )}
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

function rawEvents(item: TranscriptItem): string {
  return JSON.stringify(
    item.events.map(({ event_type, sequence_num, created_at, payload }) => ({
      event_type,
      sequence_num,
      created_at,
      payload,
    })),
    null,
    2
  );
}

function TranscriptCard({ item }: { item: TranscriptItem }): JSX.Element {
  const time = transcriptEventTime(item);
  const title =
    item.kind === "message"
      ? item.role === "user"
        ? "You"
        : "Claude"
      : item.kind === "tool-run"
        ? item.tools.length === 1
          ? (item.tools[0]?.name ?? "Tool")
          : "Tool run"
        : item.kind === "activity"
          ? item.title
          : item.kind === "thinking"
            ? "Thinking"
            : item.title;
  const color =
    item.kind === "message" ? (item.role === "user" ? "blue" : "violet") : item.kind === "tool-run" ? "cyan" : "gray";
  return (
    <Paper
      component="article"
      aria-label={title}
      data-fold-kind={item.kind}
      data-tool-count={item.kind === "tool-run" ? item.tools.length : undefined}
      data-parent-tool-use-id={item.kind === "message" || item.kind === "tool-run" ? item.parentToolUseId : undefined}
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
            {item.kind === "tool-run" && (
              <>
                <Badge variant="light" color="cyan">
                  {item.tools.length} {item.tools.length === 1 ? "tool" : "tools"}
                </Badge>
                <Badge variant="dot" color={toolStatusColor(item.status)}>
                  {item.status}
                </Badge>
              </>
            )}
            {(item.kind === "message" || item.kind === "tool-run") && item.parentToolUseId !== undefined && (
              <Badge size="xs" variant="light" color="blue">
                Subagent
              </Badge>
            )}
            {item.kind === "activity" && (
              <Badge variant="dot" color={toolStatusColor(item.status)}>
                {item.status}
              </Badge>
            )}
          </Group>
          {time !== null && (
            <Text component="time" size="xs" c="dimmed" dateTime={item.events.at(-1)?.created_at}>
              {time}
            </Text>
          )}
        </Group>
        {item.kind === "message" && (
          <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {item.text}
          </Text>
        )}
        {item.kind === "tool-run" && <ToolRun item={item} />}
        {item.kind === "activity" && item.detail !== undefined && (
          <Text size="sm" c="dimmed">
            {item.detail}
          </Text>
        )}
        {item.kind === "thinking" && (
          <Accordion variant="default" radius="sm">
            <Accordion.Item value="thinking">
              <Accordion.Control>Show thinking</Accordion.Control>
              <Accordion.Panel>
                <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
                  {item.text}
                </Text>
              </Accordion.Panel>
            </Accordion.Item>
          </Accordion>
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
        {item.kind === "notice" && item.detail !== undefined && (
          <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {item.detail}
          </Text>
        )}
        <Accordion variant="contained" radius="sm">
          <Accordion.Item value="raw-events">
            <Accordion.Control>Original event data</Accordion.Control>
            <Accordion.Panel>
              <ScrollArea type="auto" mah={384}>
                <Code block>{rawEvents(item)}</Code>
              </ScrollArea>
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      </Stack>
    </Paper>
  );
}

export function SessionViewer(): JSX.Element {
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [search, setSearch] = useState("");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [nextSessionCursor, setNextSessionCursor] = useState<string | null>(null);
  const [resumeToken, setResumeToken] = useState<string | null>(null);
  const [watchStatus, setWatchStatus] = useState<WatchStatus>("connecting");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  // A watch refresh must never remove the transcript DOM or reset its scroll/disclosures.
  const loadedSession = useRef<string | null>(null);
  const [nextEventCursor, setNextEventCursor] = useState<string | null>(null);
  const [hasMoreEvents, setHasMoreEvents] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [eventError, setEventError] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [loadingMoreSessions, setLoadingMoreSessions] = useState(false);
  const [loadingMoreEvents, setLoadingMoreEvents] = useState(false);
  const [refreshCount, setRefreshCount] = useState(0);

  useEffect(() => {
    let current = true;
    const statuses = filter === "all" ? ALL_STATUSES : [filter];
    // Only the first fetch needs a loading placeholder; watch refreshes retain the list.
    setSessionError(null);
    void listSessions(statuses)
      .then((page) => {
        if (!current) return;
        setSessions(page.data);
        setNextSessionCursor(page.next_cursor);
        setResumeToken(page.resume_token ?? null);
        setSelectedId((previous) =>
          page.data.some((session) => session.id === previous) ? previous : (page.data[0]?.id ?? null)
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
      setLoadingEvents(false);
      setEvents([]);
      setNextEventCursor(null);
      setHasMoreEvents(false);
      return () => {
        current = false;
      };
    }
    if (loadedSession.current !== selectedId) {
      loadedSession.current = selectedId;
      setLoadingEvents(true);
      setEvents([]);
      setNextEventCursor(null);
      setHasMoreEvents(false);
    }
    setEventError(null);
    void listSessionEvents(selectedId)
      .then((page) => {
        if (!current) return;
        setEvents(page.data);
        setNextEventCursor(page.has_more ? page.last_id : null);
        setHasMoreEvents(page.has_more);
      })
      .catch((reason: unknown) => {
        if (current) setEventError(errorMessage(reason));
      })
      .finally(() => {
        if (current) setLoadingEvents(false);
      });
    return () => {
      current = false;
    };
  }, [refreshCount, selectedId]);

  const visibleSessions = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return needle === ""
      ? sessions
      : sessions.filter((session) => JSON.stringify(session).toLowerCase().includes(needle));
  }, [search, sessions]);

  const selectedSession = sessions.find((session) => session.id === selectedId) ?? null;
  const transcript = useMemo(() => foldSessionEvents(events), [events]);
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
    if (selectedId === null || nextEventCursor === null || loadingMoreEvents) return;
    setLoadingMoreEvents(true);
    setEventError(null);
    try {
      const page = await listSessionEvents(selectedId, nextEventCursor);
      setEvents((previous) => [...previous, ...page.data]);
      setNextEventCursor(page.has_more ? page.last_id : null);
      setHasMoreEvents(page.has_more);
    } catch (reason) {
      setEventError(errorMessage(reason));
    } finally {
      setLoadingMoreEvents(false);
    }
  }, [loadingMoreEvents, nextEventCursor, selectedId]);

  return (
    <Paper component="div" role="region" aria-labelledby="session-viewer-title" withBorder radius="md" p="md">
      <Stack gap="md">
        <Group justify="space-between" align="center">
          <Stack gap={4}>
            <Title id="session-viewer-title" order={2} size="h3">
              Sessions
            </Title>
            <Text size="sm" c="dimmed">
              Read-only view of the synced Claude Code session history.
            </Text>
          </Stack>
          <Group gap="sm">
            {resumeToken !== null && (
              <Badge role="status" variant="dot" color={watchStatus === "connected" ? "green" : "yellow"}>
                {watchLabel}
              </Badge>
            )}
            <Button variant="default" onClick={() => setRefreshCount((count) => count + 1)}>
              Refresh
            </Button>
          </Group>
        </Group>

        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          <TextInput
            label="Search loaded sessions"
            type="search"
            value={search}
            onChange={(event) => setSearch(event.currentTarget.value)}
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
              if (value !== null) setFilter(value as StatusFilter);
            }}
          />
        </SimpleGrid>

        {sessionError !== null && (
          <Alert color="red" title="Could not load sessions">
            {sessionError}
          </Alert>
        )}

        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
          <Paper component="aside" aria-label="Session list" withBorder radius="sm" p="xs">
            <ScrollArea h="min(32rem, 60vh)" type="auto">
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
                      onSelect={() => setSelectedId(session.id)}
                    />
                  ))}
                </Stack>
              )}
              {nextSessionCursor !== null && (
                <Button
                  fullWidth
                  variant="default"
                  mt="xs"
                  loading={loadingMoreSessions}
                  onClick={() => void loadMoreSessions()}
                >
                  Load more sessions
                </Button>
              )}
            </ScrollArea>
          </Paper>

          <Paper component="div" role="region" aria-label="Session transcript" withBorder radius="sm" p="md">
            {selectedSession === null ? (
              <Center h={240}>
                <Text c="dimmed" ta="center">
                  Select a session to view its transcript.
                </Text>
              </Center>
            ) : (
              <Stack gap="md" aria-busy={loadingEvents}>
                <Group justify="space-between" align="flex-start" gap="xs">
                  <Stack gap={4}>
                    <Title order={4}>{selectedSession.title || "Untitled session"}</Title>
                    <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: "anywhere" }}>
                      {sessionSubtitle(selectedSession)}
                    </Text>
                  </Stack>
                  <Badge variant="light" color={statusColor(selectedSession.status)}>
                    {selectedSession.status}
                  </Badge>
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
                ) : transcript.length === 0 ? (
                  <Center h={180}>
                    <Text c="dimmed" ta="center">
                      No events are stored for this session yet.
                    </Text>
                  </Center>
                ) : (
                  <ScrollArea h="min(32rem, 60vh)" type="auto">
                    <Stack gap="sm" pr="sm">
                      {transcript.map((item) => (
                        <TranscriptCard key={`${item.kind}-${item.id}`} item={item} />
                      ))}
                      {hasMoreEvents && (
                        <Button variant="default" loading={loadingMoreEvents} onClick={() => void loadMoreEvents()}>
                          Load more events
                        </Button>
                      )}
                    </Stack>
                  </ScrollArea>
                )}
              </Stack>
            )}
          </Paper>
        </SimpleGrid>
      </Stack>
    </Paper>
  );
}
