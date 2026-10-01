import { useCallback, useEffect, useMemo, useState, type JSX } from "react";
import {
  Accordion,
  Alert,
  Badge,
  Button,
  Center,
  Code,
  Group,
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
import { foldSessionEvents, transcriptEventTime, type TranscriptItem } from "./transcript";

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

function toolInputSummary(input: unknown): string | null {
  if (typeof input === "string") return input;
  if (typeof input !== "object" || input === null || Array.isArray(input)) return null;
  const record = input as Record<string, unknown>;
  for (const key of ["file_path", "path", "command", "query", "pattern", "url"]) {
    if (typeof record[key] === "string") return record[key] as string;
  }
  return Object.keys(record).length === 0 ? null : JSON.stringify(record, null, 2);
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
      : item.kind === "tool"
        ? item.name
        : item.kind === "activity"
          ? item.title
          : item.kind === "thinking"
            ? "Thinking"
            : item.title;
  const color =
    item.kind === "message" ? (item.role === "user" ? "blue" : "violet") : item.kind === "tool" ? "cyan" : "gray";
  return (
    <Paper component="article" aria-label={title} data-fold-kind={item.kind} withBorder radius="sm" p="md">
      <Stack gap="sm">
        <Group justify="space-between" align="center" gap="xs">
          <Group gap="xs">
            <Badge variant="light" color={color}>
              {title}
            </Badge>
            {item.kind === "tool" && (
              <Badge
                variant="dot"
                color={item.status === "error" ? "red" : item.status === "complete" ? "green" : "yellow"}
              >
                {item.status}
              </Badge>
            )}
            {item.kind === "activity" && (
              <Badge
                variant="dot"
                color={item.status === "completed" ? "green" : item.status === "failed" ? "red" : "yellow"}
              >
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
        {item.kind === "tool" && (
          <Stack gap="xs">
            {toolInputSummary(item.input) !== null && <Code block>{toolInputSummary(item.input)}</Code>}
            {item.result !== undefined && (
              <Text size="sm" lineClamp={8} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {item.result}
              </Text>
            )}
          </Stack>
        )}
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
    setLoadingSessions(true);
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
      setEvents([]);
      setNextEventCursor(null);
      setHasMoreEvents(false);
      return () => {
        current = false;
      };
    }
    setLoadingEvents(true);
    setEventError(null);
    setEvents([]);
    setNextEventCursor(null);
    setHasMoreEvents(false);
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
