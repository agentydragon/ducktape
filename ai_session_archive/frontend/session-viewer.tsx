import { useEffect, useMemo, useRef, useState, type CSSProperties, type JSX } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Menu,
  Paper,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useLocation, useNavigate, useParams } from "react-router";
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";

import { harnessLabel, httpApi, type SessionBrowserApi } from "./api";
import { ArchiveTranscript } from "./transcript";
import { MachineImportProgressSummary, useMachineImportProgress } from "./import-progress";
import type { EventRecord } from "./schema";
import type { Harness, SessionSummary } from "./openapi_schema";
import { RelatedSessionsPanel } from "./related-sessions-panel";
import { SessionListSidebar, TranscriptStatusBadge, transcriptStatus } from "./session-list-sidebar";
import { formatDateTime } from "./session-viewer-formatting";
import {
  readSessionViewerSearch,
  SESSION_KINDS,
  sessionFilterPath,
  sessionKindsFromScope,
  sessionKindsPath,
  sessionPath as buildSessionPath,
  sessionVersionPath,
} from "./session-viewer-url";
import { usePaneSize, type PaneSize } from "../../util/frontend/resize_handle";
import { HistoryRecorderTools } from "../../util/frontend/transcript/history_recorder_tools";
import { historyRecorder } from "./history-recorder";

const SESSION_PAGE_SIZE = 50;
const SESSION_SIDEBAR_MIN_WIDTH = 240;
const SESSION_SIDEBAR_MAX_WIDTH = 520;
const SESSION_SIDEBAR_DEFAULT_WIDTH = 320;
const SESSION_TRANSCRIPT_MIN_WIDTH = 360;
const SESSION_RESIZE_HANDLE_WIDTH = 8;

function maxSessionSidebarWidth(layoutWidth: number): number {
  return Math.max(
    SESSION_SIDEBAR_MIN_WIDTH,
    Math.min(SESSION_SIDEBAR_MAX_WIDTH, layoutWidth - SESSION_TRANSCRIPT_MIN_WIDTH - SESSION_RESIZE_HANDLE_WIDTH)
  );
}

function noVersionExplanation(status: SessionSummary["transcript_status"]): string {
  switch (status) {
    case "pending":
      return "Restic inventory found a file, but no transcript version has been imported yet.";
    case "failed":
      return "Importing an inventoried file failed, so no transcript version is available.";
    case "unavailable":
      return "No transcript version is available. Inventory alone does not establish that a usable file was captured; the source may be absent or unavailable.";
    case "captured":
      return "The index reports a captured version, but no version details are available.";
  }
}

export function SessionViewer({ api = httpApi }: { api?: SessionBrowserApi }): JSX.Element {
  const { sessionId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const {
    machineId: selectedMachineId,
    harness: selectedHarness,
    kindScope,
    versionId: requestedVersionId,
  } = readSessionViewerSearch(location.search);
  const selectedKinds = useMemo(() => sessionKindsFromScope(kindScope), [kindScope]);
  const queryClient = useQueryClient();
  const layoutRef = useRef<HTMLDivElement>(null);
  const [layoutWidth, setLayoutWidth] = useState(() => Math.max(0, window.innerWidth - 32));
  const [mobileSessionListOpen, setMobileSessionListOpen] = useState(false);
  const sidebarMaxWidth = maxSessionSidebarWidth(layoutWidth);
  const storedSidebarPane = usePaneSize(
    "ai-session-archive-sidebar-width",
    SESSION_SIDEBAR_DEFAULT_WIDTH,
    SESSION_SIDEBAR_MIN_WIDTH,
    sidebarMaxWidth
  );
  const sidebarWidth = Math.min(storedSidebarPane.size, sidebarMaxWidth);
  const sidebarPane: PaneSize = { ...storedSidebarPane, size: sidebarWidth };
  const machinesQuery = useQuery({
    queryKey: ["ai-session-archive", "machines"],
    queryFn: ({ signal }) => api.listMachines(signal),
  });
  const machines = machinesQuery.data ?? [];
  const {
    progress: importProgress,
    loading: loadingImportProgress,
    error: importProgressError,
  } = useMachineImportProgress(api);
  const sessionListQueryKey = [
    "ai-session-archive",
    "sessions",
    selectedMachineId || null,
    selectedHarness || null,
    selectedKinds,
  ] as const;
  const sessionsQuery = useInfiniteQuery({
    queryKey: sessionListQueryKey,
    queryFn: ({ pageParam, signal }) =>
      api.listSessions(
        {
          ...(selectedMachineId === "" ? {} : { machineId: selectedMachineId }),
          ...(selectedHarness === "" ? {} : { harness: selectedHarness as Harness }),
          ...(pageParam === null ? {} : { cursor: pageParam }),
          kinds: selectedKinds,
          limit: SESSION_PAGE_SIZE,
        },
        signal
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: selectedKinds.length > 0,
  });
  const sessionPages = sessionsQuery.data?.pages ?? [];
  const sessions = useMemo(
    () =>
      mergeById(
        [],
        sessionPages.flatMap((page) => page.data)
      ),
    [sessionPages]
  );
  const sessionCursor = sessionPages.at(-1)?.next_cursor ?? null;
  const loadingSessions = selectedKinds.length > 0 && sessionsQuery.isLoading;
  const loadingMoreSessions = sessionsQuery.isFetchingNextPage;
  const loadedSessionScope = selectedKinds.length === 0 || sessionsQuery.data !== undefined;

  const listedSelectedSession = sessions.find((session) => session.id === sessionId) ?? null;
  const selectedSessionQuery = useQuery({
    queryKey: ["ai-session-archive", "session", sessionId ?? null],
    queryFn: ({ signal }) => {
      if (sessionId === undefined) throw new Error("A session ID is required.");
      return api.getSession(sessionId, signal);
    },
    enabled: sessionId !== undefined && listedSelectedSession === null,
  });
  const fetchedSession =
    sessionId !== undefined && selectedSessionQuery.data?.id === sessionId ? selectedSessionQuery.data : null;
  const selectedSession = listedSelectedSession ?? fetchedSession;
  const loadingSelectedSession =
    sessionId !== undefined && listedSelectedSession === null && selectedSessionQuery.isLoading;
  const selectedSessionError = selectedSessionQuery.error === null ? null : errorMessage(selectedSessionQuery.error);

  const versionsQuery = useQuery({
    queryKey: [
      "ai-session-archive",
      "versions",
      selectedSession?.id ?? null,
      selectedSession?.latest_version_id ?? null,
    ],
    queryFn: ({ signal }) => {
      if (selectedSession === null) throw new Error("A selected session is required.");
      return api.listVersions(selectedSession.id, signal);
    },
    enabled: selectedSession !== null,
  });
  const versions = versionsQuery.data ?? [];
  const hasLoadedVersions = versionsQuery.data !== undefined;
  const requestedVersionUnavailable =
    requestedVersionId !== null && hasLoadedVersions && !versions.some((version) => version.id === requestedVersionId);
  const selectedVersionId = !hasLoadedVersions
    ? null
    : requestedVersionId !== null
      ? (versions.find((version) => version.id === requestedVersionId)?.id ?? null)
      : (versions.find((version) => version.id === selectedSession?.latest_version_id)?.id ?? versions[0]?.id ?? null);
  const selectedVersion = versions.find((version) => version.id === selectedVersionId) ?? null;
  const loadingVersions = selectedSession !== null && versionsQuery.isLoading;

  const eventQuery = useInfiniteQuery({
    queryKey: ["ai-session-archive", "events", selectedSession?.id ?? null, selectedVersionId],
    queryFn: ({ pageParam, signal }) => {
      if (selectedSession === null || selectedVersionId === null) {
        throw new Error("A selected session version is required.");
      }
      return api.listEvents(selectedSession.id, selectedVersionId, pageParam, signal);
    },
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: selectedSession !== null && selectedVersionId !== null,
  });
  const eventPages = eventQuery.data?.pages ?? [];
  const events = useMemo(
    () =>
      mergeByOrdinal(
        [],
        eventPages.flatMap((page) => page.data)
      ),
    [eventPages]
  );
  const eventCursor = eventPages.at(-1)?.next_cursor ?? null;
  const loadingEvents = eventQuery.isLoading;
  const loadingEarlierEvents = eventQuery.isFetchingNextPage;
  const eventScope = `${selectedSession?.id ?? ""}\u0000${selectedVersionId ?? ""}`;

  const error =
    [machinesQuery.error, sessionsQuery.error, versionsQuery.error, eventQuery.error].find(
      (reason): reason is Error => reason !== null
    ) ?? null;
  const errorText = error === null ? null : errorMessage(error);
  const loadingMachines = machinesQuery.isLoading;

  const machineNames = useMemo(
    () => new Map(machines.map((machine) => [machine.id, machine.display_name])),
    [machines]
  );
  const selectedVersionIsEarlierCapture =
    selectedVersion !== null &&
    selectedSession !== null &&
    selectedSession.latest_version_id !== null &&
    selectedVersion.id !== selectedSession.latest_version_id;

  useEffect(() => {
    const layout = layoutRef.current;
    if (layout === null) return;
    const updateWidth = (): void => {
      if (layout.clientWidth > 0) setLayoutWidth(layout.clientWidth);
    };
    updateWidth();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", updateWidth);
      return () => window.removeEventListener("resize", updateWidth);
    }
    const observer = new ResizeObserver(updateWidth);
    observer.observe(layout);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (sessionId === undefined && loadedSessionScope && !loadingSessions && sessions[0]) {
      navigate(`/sessions/${encodeURIComponent(sessions[0].id)}${location.search}`, { replace: true });
    }
  }, [sessionId, loadedSessionScope, loadingSessions, sessions, location.search, navigate]);

  // Scroll and layout callbacks can call these again before a render publishes isFetchingNextPage.
  // cancelRefetch: false joins the in-flight page; the default would abort it and refetch.
  const loadMoreSessions = (): void => {
    if (sessionsQuery.hasNextPage) void sessionsQuery.fetchNextPage({ cancelRefetch: false });
  };

  const loadEarlierEvents = (): void => {
    if (eventQuery.hasNextPage) void eventQuery.fetchNextPage({ cancelRefetch: false });
  };

  const sessionPath = (targetId: string): string => {
    return buildSessionPath(targetId, location.search);
  };

  const changeFilter = (name: "machine" | "harness", value: string): void => {
    navigate(sessionFilterPath(location.search, name, value));
  };

  const changeKinds = (values: string[]): void => {
    navigate(sessionKindsPath(location.pathname, location.search, values));
  };

  const changeVersion = (versionId: string): void => {
    const nextPath = sessionVersionPath(location.pathname, location.search, versionId);
    if (nextPath !== `${location.pathname}${location.search}`) navigate(nextPath);
  };

  const sessionListSidebarModel = {
    filters: {
      machines,
      machineId: selectedMachineId,
      harness: selectedHarness,
      kinds: selectedKinds,
      loadingMachines,
      onChange: changeFilter,
      onKindsChange: changeKinds,
    },
    sessions: {
      items: sessions,
      machineNames,
      selectedId: sessionId,
      pathFor: sessionPath,
      nextCursor: sessionCursor,
      loading: loadingSessions,
      loadingNext: loadingMoreSessions,
      onLoadMore: loadMoreSessions,
      onRefresh: () => void queryClient.resetQueries({ queryKey: sessionListQueryKey, exact: true }),
    },
    layout: {
      mobileOpen: mobileSessionListOpen,
      setMobileOpen: setMobileSessionListOpen,
      sidebarWidth,
      sidebarPane,
    },
  };

  return (
    <Stack gap="sm" style={{ height: "100%", minHeight: 0 }}>
      <Box component="header" className="session-browser-header">
        <Box>
          <Group gap="xs" justify="space-between" wrap="nowrap">
            <Title order={1} size="h3">
              Local agent sessions
            </Title>
            <HistoryRecorderTools
              recorder={historyRecorder}
              label="Transcript recorder"
              downloadPrefix="ai-session-archive-history-recording"
              position="bottom-end"
            />
          </Group>
          <Text size="sm" c="dimmed">
            Captured Claude Code and Codex CLI conversations
          </Text>
        </Box>
        <MachineImportProgressSummary
          progress={importProgress}
          loading={loadingImportProgress}
          error={importProgressError}
          returnTo={`${location.pathname}${location.search}`}
        />
      </Box>
      {errorText !== null && (
        <Alert color="red" title="Session data unavailable">
          {errorText}
        </Alert>
      )}
      <Box
        ref={layoutRef}
        className="session-browser-layout"
        style={{ "--session-sidebar-width": `${sidebarWidth}px` } as CSSProperties}
      >
        <SessionListSidebar model={sessionListSidebarModel} />

        <Paper
          component="section"
          aria-label="Session transcript"
          withBorder
          radius="sm"
          p="md"
          className="session-browser-transcript"
        >
          <Group className="session-browser-mobile-toolbar" gap="xs" wrap="wrap">
            <Button
              className="session-browser-mobile-toggle"
              variant="default"
              size="xs"
              aria-controls="session-browser-sidebar"
              aria-expanded={mobileSessionListOpen}
              onClick={() => setMobileSessionListOpen(true)}
            >
              Session list
            </Button>
          </Group>
          {selectedSession === null ? (
            <Center style={{ flex: 1 }}>
              {loadingSelectedSession ? (
                <Loader size="sm" aria-label="Loading selected session" />
              ) : (
                <Text c="dimmed">
                  {selectedSessionError !== null
                    ? `Could not load session ${sessionId}: ${selectedSessionError}`
                    : "Select a session to view its captured versions."}
                </Text>
              )}
            </Center>
          ) : (
            <Stack gap="md" style={{ flex: 1, minHeight: 0 }}>
              <Group justify="space-between" align="start" wrap="nowrap">
                <Box style={{ minWidth: 0, flex: 1 }}>
                  <Group gap="xs" mb={4}>
                    <Badge variant="light">{harnessLabel(selectedSession.harness)}</Badge>
                    <Text size="sm" c="dimmed">
                      {machineNames.get(selectedSession.machine_id) ?? selectedSession.machine_id}
                    </Text>
                    {selectedSession.identity_inferred && (
                      <Badge color="orange" variant="light">
                        Identity inferred
                      </Badge>
                    )}
                    <TranscriptStatusBadge session={selectedSession} size="sm" />
                    {selectedVersionIsEarlierCapture && (
                      <Badge size="xs" color="orange" variant="light">
                        Earlier capture
                      </Badge>
                    )}
                    {selectedVersion !== null && selectedVersion.parse_issue_count > 0 && (
                      <Badge size="xs" color="orange" variant="light">
                        {selectedVersion.parse_issue_count} parse warning
                        {selectedVersion.parse_issue_count === 1 ? "" : "s"}
                      </Badge>
                    )}
                    {selectedSession.native_session_id_conflict && (
                      <Badge color="red" variant="light">
                        Conflicting native session IDs
                      </Badge>
                    )}
                  </Group>
                  <Group gap="xs" wrap="nowrap" align="center">
                    <Title order={2} size="h4" style={{ flex: 1, minWidth: 0, overflowWrap: "anywhere" }}>
                      {selectedSession.title || "Untitled session"}
                    </Title>
                    <RelatedSessionsPanel
                      api={api}
                      session={selectedSession}
                      layoutWidth={layoutWidth}
                      pathForSession={sessionPath}
                      onNavigate={() => setMobileSessionListOpen(false)}
                    />
                  </Group>
                  <Group gap="xs" align="baseline">
                    {selectedSession.cwd !== null && (
                      <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: "anywhere" }}>
                        {selectedSession.cwd}
                      </Text>
                    )}
                    <Text size="xs" c="dimmed">
                      Started {formatDateTime(selectedSession.started_at)} · Last active{" "}
                      {formatDateTime(selectedSession.last_activity_at)}
                    </Text>
                  </Group>
                  {selectedSession.native_session_id_conflict && (
                    <Text size="xs" c="red" mt={4} style={{ overflowWrap: "anywhere" }}>
                      Native session ID candidates:{" "}
                      {(selectedSession.native_session_id_candidates ?? []).join(", ") || "unavailable"}
                    </Text>
                  )}
                </Box>
                <Group gap="xs" wrap="nowrap" style={{ flex: "0 0 auto" }}>
                  <Menu position="bottom-end" width="min(90vw, 420px)" withinPortal>
                    <Menu.Target>
                      <ActionIcon
                        size="sm"
                        variant="subtle"
                        color="gray"
                        aria-label="Conversation options"
                        title="Conversation options"
                      >
                        <IconDotsVertical size={16} />
                      </ActionIcon>
                    </Menu.Target>
                    <Menu.Dropdown style={{ maxHeight: "min(70dvh, 480px)", overflowY: "auto" }}>
                      <Menu.Label>Captured versions</Menu.Label>
                      {loadingVersions ? (
                        <Menu.Item disabled>Loading versions…</Menu.Item>
                      ) : (
                        <>
                          {requestedVersionUnavailable && <Menu.Item disabled>Requested capture unavailable</Menu.Item>}
                          {versions.map((version) => (
                            <Menu.RadioItem
                              key={version.id}
                              value={version.id}
                              data-version-id={version.id}
                              checked={selectedVersionId === version.id}
                              closeMenuOnClick
                              style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}
                              onClick={() =>
                                changeVersion(version.id === selectedSession.latest_version_id ? "" : version.id)
                              }
                            >
                              {`${version.id === selectedSession.latest_version_id ? "Latest captured · " : ""}${formatDateTime(version.captured_at)} · ${formatBytes(version.byte_size)}`}
                            </Menu.RadioItem>
                          ))}
                          {versions.length === 0 && !requestedVersionUnavailable && (
                            <Menu.Item disabled>No captured versions</Menu.Item>
                          )}
                        </>
                      )}
                      {selectedVersion !== null && (
                        <>
                          <Menu.Divider />
                          <Menu.Label>
                            <Stack gap={2}>
                              <Text size="xs">
                                {selectedVersion.complete ? "All JSONL lines parsed" : "Some JSONL lines did not parse"}
                              </Text>
                              <Text size="xs" c="dimmed">
                                {selectedVersion.parse_issue_count} parse issues · SHA-256{" "}
                                {selectedVersion.sha256.slice(0, 12)}
                              </Text>
                            </Stack>
                          </Menu.Label>
                        </>
                      )}
                    </Menu.Dropdown>
                  </Menu>
                </Group>
              </Group>
              {requestedVersionUnavailable ? (
                <Center h={180}>
                  <Stack align="center" gap="xs" maw={480}>
                    <Text size="sm" fw={600}>
                      Requested capture unavailable
                    </Text>
                    <Text size="sm" c="dimmed" ta="center">
                      This captured version is not available for this session. Choose another version or view the
                      current capture.
                    </Text>
                    <Button variant="light" size="xs" onClick={() => changeVersion("")}>
                      View current capture
                    </Button>
                  </Stack>
                </Center>
              ) : loadingVersions ? (
                <Center style={{ flex: 1 }}>
                  <Loader size="sm" aria-label="Loading captured versions" />
                </Center>
              ) : selectedVersion === null ? (
                <Center h={180}>
                  <Stack align="center" gap="xs" maw={480}>
                    <Text size="sm" fw={600}>
                      {transcriptStatus(selectedSession)?.label}
                    </Text>
                    <Text size="sm" c="dimmed" ta="center">
                      {noVersionExplanation(selectedSession.transcript_status)}
                    </Text>
                  </Stack>
                </Center>
              ) : loadingEvents && events.length === 0 ? (
                <Center style={{ flex: 1 }}>
                  <Loader size="sm" aria-label="Loading conversation records" />
                </Center>
              ) : (
                <ArchiveTranscript
                  scopeId={eventScope}
                  records={events}
                  earlier={{
                    available: eventCursor !== null,
                    loading: loadingEarlierEvents,
                    load: () => void loadEarlierEvents(),
                  }}
                  tail={
                    <>
                      {events.length === 0 && !loadingEvents && (
                        <Text size="sm" c="dimmed">
                          This captured version contains no JSONL records.
                        </Text>
                      )}
                    </>
                  }
                />
              )}
            </Stack>
          )}
        </Paper>
      </Box>
    </Stack>
  );
}

function mergeById<T extends { id: string }>(previous: T[], incoming: T[]): T[] {
  const byId = new Map(previous.map((item) => [item.id, item]));
  for (const item of incoming) byId.set(item.id, item);
  return [...byId.values()];
}

function mergeByOrdinal(previous: EventRecord[], incoming: EventRecord[]): EventRecord[] {
  const byOrdinal = new Map(previous.map((event) => [event.ordinal, event]));
  for (const event of incoming) byOrdinal.set(event.ordinal, event);
  return [...byOrdinal.values()].sort((left, right) => left.ordinal - right.ordinal);
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes;
  for (const unit of units) {
    value /= 1024;
    if (value < 1024 || unit === "GB") return `${value.toFixed(value < 10 ? 1 : 0)} ${unit}`;
  }
  return `${bytes} B`;
}

function errorMessage(reason: unknown, fallback = "Could not load local sessions."): string {
  return reason instanceof Error ? reason.message : fallback;
}
