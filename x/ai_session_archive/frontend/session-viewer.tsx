import { useEffect, useMemo, useRef, useState, type CSSProperties, type JSX } from "react";
import {
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  NativeSelect,
  Paper,
  ScrollArea,
  Select,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { harnessLabel, httpApi, type SessionBrowserApi } from "./api";
import { recordLabel, RecordContent, rawRecordText } from "./event-content";
import { MachineImportProgressSummary, useMachineImportProgress } from "./import-progress";
import type { EventRecord, Harness, Machine, SessionSummary, SessionVersion } from "./schema";
import { ResizeHandle, usePaneSize, type PaneSize } from "../../../util/frontend/resize_handle";

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

function parentLinkLabel(kind: string | null): string | null {
  switch (kind) {
    case null:
      return null;
    case "claude_path_inferred":
      return "Parent inferred from Claude subagent path";
    case "codex_parent_thread_id":
      return "Parent linked from Codex parent_thread_id";
    case "codex_parent_thread_id_unresolved":
      return "Codex parent_thread_id is unresolved";
    case "codex_parent_thread_id_ambiguous":
      return "Codex parent_thread_id is ambiguous";
    case "codex_parent_thread_id_inconsistent":
      return "Conflicting Codex parent_thread_id values";
    default:
      return `Parent link source: ${kind.replaceAll("_", " ")}`;
  }
}

function transcriptStatusLabel(session: SessionSummary): string {
  switch (session.transcript_status) {
    case "captured":
      return session.latest_version_record_count === 0 ? "Latest capture empty" : "Captured";
    case "pending":
      return "Inventory only";
    case "failed":
      return "Import failed";
    case "unavailable":
      return "No captured version";
  }
}

function transcriptStatusColor(session: SessionSummary): string {
  switch (session.transcript_status) {
    case "captured":
      return "green";
    case "pending":
      return "yellow";
    case "failed":
      return "red";
    case "unavailable":
      return "gray";
  }
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
  const search = new URLSearchParams(location.search);
  const selectedMachineId = search.get("machine") ?? "";
  const selectedHarness = search.get("harness") ?? "";
  const requestedVersionId = search.get("version");
  const layoutRef = useRef<HTMLDivElement>(null);
  const mobileSessionListRef = useRef<HTMLElement>(null);
  const relatedSessionsRef = useRef<HTMLElement>(null);
  const focusRelatedSessionsOnOpenRef = useRef(false);
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
  const sidebarPane: PaneSize = {
    ...storedSidebarPane,
    size: sidebarWidth,
    max: sidebarMaxWidth,
    resize: (size) => storedSidebarPane.resize(Math.min(size, sidebarMaxWidth)),
    step: (delta) => storedSidebarPane.step(delta),
  };
  const [machines, setMachines] = useState<Machine[]>([]);
  const {
    progress: importProgress,
    loading: loadingImportProgress,
    error: importProgressError,
  } = useMachineImportProgress(api);
  const [sessionListRefreshRequest, setSessionListRefreshRequest] = useState(0);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loadedSessionScope, setLoadedSessionScope] = useState<string | null>(null);
  const [sessionCursor, setSessionCursor] = useState<string | null>(null);
  const [fetchedSession, setFetchedSession] = useState<SessionSummary | null>(null);
  const [loadingSelectedSession, setLoadingSelectedSession] = useState(false);
  const [selectedSessionError, setSelectedSessionError] = useState<string | null>(null);
  const [versions, setVersions] = useState<SessionVersion[]>([]);
  const [versionsSessionId, setVersionsSessionId] = useState<string | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [eventCursor, setEventCursor] = useState<number | null>(null);
  const [parentSession, setParentSession] = useState<SessionSummary | null>(null);
  const [childSessions, setChildSessions] = useState<SessionSummary[]>([]);
  const [childSessionCursor, setChildSessionCursor] = useState<string | null>(null);
  const [loadingMachines, setLoadingMachines] = useState(true);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingMoreSessions, setLoadingMoreSessions] = useState(false);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [loadingMoreEvents, setLoadingMoreEvents] = useState(false);
  const [loadingRelatedSessions, setLoadingRelatedSessions] = useState(false);
  const [loadingMoreRelatedSessions, setLoadingMoreRelatedSessions] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionScope = `${selectedMachineId}\u0000${selectedHarness}`;
  const sessionScopeRef = useRef(sessionScope);
  sessionScopeRef.current = sessionScope;
  const selectedSession =
    sessions.find((session) => session.id === sessionId) ?? (fetchedSession?.id === sessionId ? fetchedSession : null);
  const requestedVersionUnavailable =
    requestedVersionId !== null &&
    versionsSessionId === selectedSession?.id &&
    !versions.some((version) => version.id === requestedVersionId);
  const selectedVersionId =
    versionsSessionId === selectedSession?.id
      ? requestedVersionId !== null
        ? (versions.find((version) => version.id === requestedVersionId)?.id ?? null)
        : (versions.find((version) => version.id === selectedSession.latest_version_id)?.id ?? versions[0]?.id ?? null)
      : null;
  const eventScope = `${sessionId ?? ""}\u0000${selectedVersionId ?? ""}`;
  const eventScopeRef = useRef(eventScope);
  eventScopeRef.current = eventScope;
  const sessionPageRequest = useRef<AbortController | null>(null);
  const eventPageRequest = useRef<AbortController | null>(null);
  const relatedPageRequest = useRef<AbortController | null>(null);
  const selectedSessionIdRef = useRef(sessionId);
  selectedSessionIdRef.current = sessionId;

  const machineNames = useMemo(
    () => new Map(machines.map((machine) => [machine.id, machine.display_name])),
    [machines]
  );
  const selectedVersion =
    versionsSessionId === selectedSession?.id
      ? (versions.find((version) => version.id === selectedVersionId) ?? null)
      : null;
  const hasRelatedSessions =
    selectedSession !== null &&
    (selectedSession.parent_session_id !== null ||
      selectedSession.parent_link_kind !== null ||
      childSessions.length > 0 ||
      loadingRelatedSessions);

  useEffect(
    () => () => {
      sessionPageRequest.current?.abort();
      eventPageRequest.current?.abort();
      relatedPageRequest.current?.abort();
    },
    []
  );

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
    if (!mobileSessionListOpen) return;
    const previousFocus = document.activeElement;
    const sidebar = mobileSessionListRef.current;
    const focusableElements = (): HTMLElement[] => [
      ...(sidebar?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      ) ?? []),
    ];
    if (focusRelatedSessionsOnOpenRef.current && relatedSessionsRef.current !== null) {
      focusRelatedSessionsOnOpenRef.current = false;
      relatedSessionsRef.current.scrollIntoView?.({ block: "nearest" });
      relatedSessionsRef.current.focus();
    } else {
      focusableElements()[0]?.focus();
    }
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        setMobileSessionListOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = focusableElements();
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, [mobileSessionListOpen]);

  useEffect(() => {
    const controller = new AbortController();
    void api
      .listMachines(controller.signal)
      .then(setMachines)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingMachines(false);
      });
    return () => controller.abort();
  }, [api]);

  useEffect(() => {
    sessionPageRequest.current?.abort();
    sessionPageRequest.current = null;
    const controller = new AbortController();
    setLoadingSessions(true);
    setLoadingMoreSessions(false);
    setError(null);
    void api
      .listSessions(
        {
          ...(selectedMachineId === "" ? {} : { machineId: selectedMachineId }),
          ...(selectedHarness === "" ? {} : { harness: selectedHarness as Harness }),
          limit: SESSION_PAGE_SIZE,
        },
        controller.signal
      )
      .then((page) => {
        if (controller.signal.aborted) return;
        setSessions(page.data);
        setSessionCursor(page.next_cursor);
        setLoadedSessionScope(sessionScope);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingSessions(false);
      });
    return () => {
      controller.abort();
      sessionPageRequest.current?.abort();
      sessionPageRequest.current = null;
    };
  }, [api, selectedMachineId, selectedHarness, sessionListRefreshRequest]);

  useEffect(() => {
    if (sessionId === undefined && loadedSessionScope === sessionScope && !loadingSessions && sessions[0]) {
      navigate(`/sessions/${encodeURIComponent(sessions[0].id)}${location.search}`, { replace: true });
    }
  }, [sessionId, loadedSessionScope, sessionScope, loadingSessions, sessions, location.search, navigate]);

  useEffect(() => {
    if (sessionId === undefined || sessions.some((session) => session.id === sessionId)) {
      setFetchedSession(null);
      setSelectedSessionError(null);
      setLoadingSelectedSession(false);
      return;
    }
    const controller = new AbortController();
    setFetchedSession(null);
    setSelectedSessionError(null);
    setLoadingSelectedSession(true);
    void api
      .getSession(sessionId, controller.signal)
      .then((session) => {
        if (!controller.signal.aborted) setFetchedSession(session);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setSelectedSessionError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingSelectedSession(false);
      });
    return () => controller.abort();
  }, [api, sessionId, sessions]);

  useEffect(() => {
    eventPageRequest.current?.abort();
    eventPageRequest.current = null;
    setLoadingMoreEvents(false);
    if (selectedSession === null) {
      setVersions([]);
      setVersionsSessionId(null);
      setEvents([]);
      setEventCursor(null);
      return;
    }
    const controller = new AbortController();
    setVersions([]);
    setVersionsSessionId(null);
    setEvents([]);
    setEventCursor(null);
    setLoadingVersions(true);
    setError(null);
    void api
      .listVersions(selectedSession.id, controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return;
        setVersions(items);
        setVersionsSessionId(selectedSession.id);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingVersions(false);
      });
    return () => controller.abort();
  }, [api, selectedSession?.id, selectedSession?.latest_version_id]);

  useEffect(() => {
    eventPageRequest.current?.abort();
    eventPageRequest.current = null;
    setLoadingMoreEvents(false);
    if (selectedSession === null || selectedVersionId === null || versionsSessionId !== selectedSession.id) {
      setEvents([]);
      setEventCursor(null);
      setLoadingEvents(false);
      return;
    }
    const controller = new AbortController();
    setEvents([]);
    setEventCursor(null);
    setLoadingEvents(true);
    setError(null);
    void api
      .listEvents(selectedSession.id, selectedVersionId, undefined, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        setEvents(page.data);
        setEventCursor(page.next_cursor);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingEvents(false);
      });
    return () => {
      controller.abort();
      eventPageRequest.current?.abort();
      eventPageRequest.current = null;
    };
  }, [api, selectedSession?.id, selectedVersionId, versionsSessionId]);

  useEffect(() => {
    relatedPageRequest.current?.abort();
    relatedPageRequest.current = null;
    setLoadingMoreRelatedSessions(false);
    setParentSession(null);
    setChildSessions([]);
    setChildSessionCursor(null);
    if (selectedSession === null) {
      setLoadingRelatedSessions(false);
      return;
    }

    const controller = new AbortController();
    setLoadingRelatedSessions(true);
    const parent =
      selectedSession.parent_session_id === null
        ? Promise.resolve()
        : api.getSession(selectedSession.parent_session_id, controller.signal).then((item) => {
            if (!controller.signal.aborted) setParentSession(item);
          });
    const children = api
      .listSessions({ parentSessionId: selectedSession.id, limit: SESSION_PAGE_SIZE }, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        setChildSessions(page.data);
        setChildSessionCursor(page.next_cursor);
      });
    void Promise.all([parent, children])
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingRelatedSessions(false);
      });
    return () => {
      controller.abort();
      relatedPageRequest.current?.abort();
      relatedPageRequest.current = null;
    };
  }, [api, selectedSession?.id, selectedSession?.parent_session_id]);

  const loadMoreSessions = async (): Promise<void> => {
    if (sessionCursor === null || loadingMoreSessions) return;
    const requestedScope = sessionScope;
    const controller = new AbortController();
    sessionPageRequest.current?.abort();
    sessionPageRequest.current = controller;
    setLoadingMoreSessions(true);
    setError(null);
    try {
      const page = await api.listSessions(
        {
          ...(selectedMachineId === "" ? {} : { machineId: selectedMachineId }),
          ...(selectedHarness === "" ? {} : { harness: selectedHarness as Harness }),
          cursor: sessionCursor,
          limit: SESSION_PAGE_SIZE,
        },
        controller.signal
      );
      if (!controller.signal.aborted && requestedScope === sessionScopeRef.current) {
        setSessions((current) => mergeById(current, page.data));
        setSessionCursor(page.next_cursor);
      }
    } catch (reason) {
      if (!controller.signal.aborted && requestedScope === sessionScopeRef.current) setError(errorMessage(reason));
    } finally {
      if (sessionPageRequest.current === controller) {
        sessionPageRequest.current = null;
        setLoadingMoreSessions(false);
      }
    }
  };

  const loadMoreEvents = async (): Promise<void> => {
    if (selectedSession === null || selectedVersionId === null || eventCursor === null || loadingMoreEvents) return;
    const requestedScope = eventScope;
    const controller = new AbortController();
    eventPageRequest.current?.abort();
    eventPageRequest.current = controller;
    setLoadingMoreEvents(true);
    setError(null);
    try {
      const page = await api.listEvents(selectedSession.id, selectedVersionId, eventCursor, controller.signal);
      if (!controller.signal.aborted && requestedScope === eventScopeRef.current) {
        setEvents((current) => mergeByOrdinal(current, page.data));
        setEventCursor(page.next_cursor);
      }
    } catch (reason) {
      if (!controller.signal.aborted && requestedScope === eventScopeRef.current) setError(errorMessage(reason));
    } finally {
      if (eventPageRequest.current === controller) {
        eventPageRequest.current = null;
        setLoadingMoreEvents(false);
      }
    }
  };

  const loadMoreRelatedSessions = async (): Promise<void> => {
    if (selectedSession === null || childSessionCursor === null || loadingMoreRelatedSessions) return;
    const requestedSessionId = selectedSession.id;
    const controller = new AbortController();
    relatedPageRequest.current?.abort();
    relatedPageRequest.current = controller;
    setLoadingMoreRelatedSessions(true);
    try {
      const page = await api.listSessions(
        { parentSessionId: requestedSessionId, cursor: childSessionCursor, limit: SESSION_PAGE_SIZE },
        controller.signal
      );
      if (!controller.signal.aborted && requestedSessionId === selectedSessionIdRef.current) {
        setChildSessions((current) => mergeById(current, page.data));
        setChildSessionCursor(page.next_cursor);
      }
    } catch (reason) {
      if (!controller.signal.aborted && requestedSessionId === selectedSessionIdRef.current) {
        setError(errorMessage(reason));
      }
    } finally {
      if (relatedPageRequest.current === controller) {
        relatedPageRequest.current = null;
        setLoadingMoreRelatedSessions(false);
      }
    }
  };

  const sessionPath = (targetId: string): string => {
    const params = new URLSearchParams(location.search);
    params.delete("version");
    const query = params.toString();
    return `/sessions/${encodeURIComponent(targetId)}${query ? `?${query}` : ""}`;
  };

  const changeFilter = (name: "machine" | "harness", value: string): void => {
    if (value) search.set(name, value);
    else search.delete(name);
    search.delete("version");
    navigate(`/?${search.toString()}`);
  };

  const changeVersion = (versionId: string): void => {
    if (versionId) search.set("version", versionId);
    else search.delete("version");
    navigate(`${location.pathname}?${search.toString()}`);
  };

  const openMobileRelatedSessions = (): void => {
    focusRelatedSessionsOnOpenRef.current = true;
    setMobileSessionListOpen(true);
  };

  return (
    <Stack gap="sm" style={{ height: "100%", minHeight: 0 }}>
      <Group justify="space-between" align="end" wrap="wrap">
        <Box>
          <Title order={1} size="h3">
            Local agent sessions
          </Title>
          <Text size="sm" c="dimmed">
            Captured Claude Code and Codex CLI conversations
          </Text>
        </Box>
        <Text size="xs" c="dimmed">
          Sorted by last activity
        </Text>
      </Group>
      {error !== null && (
        <Alert color="red" title="Session data unavailable">
          {error}
        </Alert>
      )}
      <MachineImportProgressSummary
        progress={importProgress}
        loading={loadingImportProgress}
        error={importProgressError}
        returnTo={`${location.pathname}${location.search}`}
      />
      <Box
        ref={layoutRef}
        className="session-browser-layout"
        style={{ "--session-sidebar-width": `${sidebarWidth}px` } as CSSProperties}
      >
        {mobileSessionListOpen && (
          <button
            type="button"
            className="session-browser-mobile-backdrop"
            aria-label="Close session list"
            tabIndex={-1}
            onClick={() => setMobileSessionListOpen(false)}
          />
        )}
        <Paper
          component="aside"
          ref={mobileSessionListRef}
          aria-label="Session list"
          aria-modal={mobileSessionListOpen || undefined}
          role={mobileSessionListOpen ? "dialog" : "complementary"}
          id="session-browser-sidebar"
          withBorder
          radius="sm"
          p="xs"
          className={`session-browser-sidebar${mobileSessionListOpen ? " session-browser-sidebar-open" : ""}`}
        >
          <Stack gap="xs" style={{ height: "100%", minHeight: 0 }}>
            <Group className="session-browser-mobile-sidebar-heading" justify="space-between" align="center">
              <Text size="sm" fw={600}>
                Session list
              </Text>
              <Button size="xs" variant="default" onClick={() => setMobileSessionListOpen(false)}>
                Close
              </Button>
            </Group>
            <Select
              label="Machine"
              aria-label="Filter by machine"
              data={[
                { value: "", label: "All machines" },
                ...machines.map((machine) => ({ value: machine.id, label: machine.display_name })),
              ]}
              value={selectedMachineId}
              onChange={(value) => changeFilter("machine", value ?? "")}
              disabled={loadingMachines}
              placeholder={loadingMachines ? "Loading machines…" : "All machines"}
            />
            <NativeSelect
              label="Harness"
              aria-label="Filter by harness"
              value={selectedHarness}
              onChange={(event) => changeFilter("harness", event.currentTarget.value)}
              data={[
                { value: "", label: "All harnesses" },
                { value: "claude", label: "Claude Code" },
                { value: "codex", label: "Codex CLI" },
              ]}
            />
            <Button
              size="xs"
              variant="default"
              loading={loadingSessions}
              onClick={() => setSessionListRefreshRequest((current) => current + 1)}
            >
              Refresh session list
            </Button>
            {hasRelatedSessions && (
              <Button
                size="xs"
                variant="subtle"
                aria-controls="session-browser-related-sessions"
                onClick={() => {
                  relatedSessionsRef.current?.scrollIntoView?.({ block: "nearest" });
                  relatedSessionsRef.current?.focus();
                }}
              >
                Jump to related sessions
              </Button>
            )}
            <ScrollArea style={{ flex: 1, minHeight: 0 }} type="auto">
              {loadingSessions ? (
                <Center h={200}>
                  <Loader size="sm" aria-label="Loading sessions" />
                </Center>
              ) : sessions.length === 0 ? (
                <Center h={160} px="md">
                  <Text size="sm" c="dimmed" ta="center">
                    No captured sessions match these filters.
                  </Text>
                </Center>
              ) : (
                <Stack gap={4}>
                  {sessions.map((session) => (
                    <SessionRow
                      key={session.id}
                      session={session}
                      machineName={machineNames.get(session.machine_id) ?? session.machine_id}
                      selected={session.id === sessionId}
                      to={sessionPath(session.id)}
                      onClick={() => setMobileSessionListOpen(false)}
                    />
                  ))}
                </Stack>
              )}
              {sessionCursor !== null && (
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
              {hasRelatedSessions && selectedSession !== null && (
                <Paper
                  component="section"
                  withBorder
                  radius="sm"
                  p="xs"
                  aria-label="Related sessions"
                  id="session-browser-related-sessions"
                  tabIndex={0}
                  ref={relatedSessionsRef}
                  className="session-browser-related-sessions"
                >
                  <Stack gap="xs">
                    <Text size="sm" fw={600}>
                      Related sessions
                    </Text>
                    {(selectedSession.parent_session_id !== null || selectedSession.parent_link_kind !== null) && (
                      <Group gap="xs" wrap="wrap">
                        <Text size="xs" c="dimmed">
                          Parent
                        </Text>
                        {selectedSession.parent_session_id !== null && parentSession !== null ? (
                          <Button
                            component={Link}
                            to={sessionPath(parentSession.id)}
                            variant="subtle"
                            size="xs"
                            onClick={() => setMobileSessionListOpen(false)}
                          >
                            {parentSession.title || "Untitled parent session"}
                          </Button>
                        ) : selectedSession.parent_session_id !== null ? (
                          <Loader size="xs" aria-label="Loading parent session" />
                        ) : (
                          <Text size="xs" c="dimmed">
                            Parent session could not be resolved to a captured session.
                          </Text>
                        )}
                        {parentLinkLabel(selectedSession.parent_link_kind) !== null && (
                          <Badge color="orange" variant="light">
                            {parentLinkLabel(selectedSession.parent_link_kind)}
                          </Badge>
                        )}
                        {typeof selectedSession.parent_native_session_id === "string" && (
                          <Text size="xs" c="dimmed">
                            Native parent ID: {selectedSession.parent_native_session_id}
                          </Text>
                        )}
                        {selectedSession.parent_native_session_id_conflict && (
                          <Text size="xs" c="red" style={{ overflowWrap: "anywhere" }}>
                            Conflicting native parent ID candidates:{" "}
                            {(selectedSession.parent_native_session_id_candidates ?? []).join(", ") || "unavailable"}
                          </Text>
                        )}
                        {parentSession?.identity_inferred && (
                          <Text size="xs" c="orange">
                            Parent transcript is not captured; its identity is inferred.
                          </Text>
                        )}
                      </Group>
                    )}
                    {childSessions.length > 0 && (
                      <Stack gap={2}>
                        <Text size="xs" c="dimmed">
                          Child sessions
                        </Text>
                        <div
                          className="session-browser-related-children"
                          role="region"
                          aria-label="Child sessions"
                          tabIndex={0}
                        >
                          <Stack gap={2} role="list">
                            {childSessions.map((child) => (
                              <Group key={child.id} gap="xs" wrap="wrap" role="listitem">
                                <Button
                                  component={Link}
                                  to={sessionPath(child.id)}
                                  variant="subtle"
                                  size="xs"
                                  onClick={() => setMobileSessionListOpen(false)}
                                >
                                  {child.title || "Untitled child session"}
                                </Button>
                                {parentLinkLabel(child.parent_link_kind) !== null && (
                                  <Badge color="orange" variant="light">
                                    {parentLinkLabel(child.parent_link_kind)}
                                  </Badge>
                                )}
                                <Text size="xs" c="dimmed">
                                  {formatDateTime(child.last_activity_at)}
                                </Text>
                              </Group>
                            ))}
                          </Stack>
                        </div>
                      </Stack>
                    )}
                    {loadingRelatedSessions && <Loader size="xs" aria-label="Loading related sessions" />}
                    {childSessionCursor !== null && (
                      <Button
                        variant="default"
                        size="xs"
                        loading={loadingMoreRelatedSessions}
                        onClick={() => void loadMoreRelatedSessions()}
                      >
                        Load more child sessions
                      </Button>
                    )}
                  </Stack>
                </Paper>
              )}
            </ScrollArea>
          </Stack>
        </Paper>

        <ResizeHandle
          pane={sidebarPane}
          axis="x"
          className="session-browser-resize-handle"
          aria-label="Resize session list"
          aria-controls="session-browser-sidebar"
          aria-valuetext={`${sidebarWidth} pixels`}
        />

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
            {hasRelatedSessions && (
              <Button
                className="session-browser-mobile-related-toggle"
                variant="default"
                size="xs"
                aria-label={`Related sessions (${childSessions.length} child sessions)`}
                aria-controls="session-browser-sidebar"
                aria-expanded={mobileSessionListOpen}
                onClick={openMobileRelatedSessions}
              >
                Related sessions{childSessions.length > 0 ? ` (${childSessions.length})` : ""}
              </Button>
            )}
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
              <Group justify="space-between" align="start" wrap="wrap">
                <Box style={{ minWidth: 240, flex: 1 }}>
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
                    <Badge color={transcriptStatusColor(selectedSession)} variant="light">
                      {transcriptStatusLabel(selectedSession)}
                    </Badge>
                    {selectedSession.native_session_id_conflict && (
                      <Badge color="red" variant="light">
                        Conflicting native session IDs
                      </Badge>
                    )}
                  </Group>
                  <Title order={2} size="h4" style={{ overflowWrap: "anywhere" }}>
                    {selectedSession.title || "Untitled session"}
                  </Title>
                  {selectedSession.cwd !== null && (
                    <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: "anywhere" }}>
                      {selectedSession.cwd}
                    </Text>
                  )}
                  <Text size="xs" c="dimmed" mt={4}>
                    Started {formatDateTime(selectedSession.started_at)} · Last active{" "}
                    {formatDateTime(selectedSession.last_activity_at)}
                  </Text>
                  {selectedSession.native_session_id_conflict && (
                    <Text size="xs" c="red" mt={4} style={{ overflowWrap: "anywhere" }}>
                      Native session ID candidates:{" "}
                      {(selectedSession.native_session_id_candidates ?? []).join(", ") || "unavailable"}
                    </Text>
                  )}
                </Box>
                <Box style={{ width: "min(100%, 360px)" }}>
                  <NativeSelect
                    label="Captured version"
                    aria-label="Captured version"
                    value={requestedVersionUnavailable ? (requestedVersionId ?? "") : (selectedVersionId ?? "")}
                    onChange={(event) => changeVersion(event.currentTarget.value)}
                    disabled={loadingVersions || (versions.length === 0 && !requestedVersionUnavailable)}
                    data={
                      loadingVersions
                        ? [{ value: "", label: "Loading versions…" }]
                        : [
                            ...(requestedVersionUnavailable
                              ? [{ value: requestedVersionId ?? "", label: "Requested capture unavailable" }]
                              : []),
                            ...versions.map((version) => ({
                              value: version.id,
                              label: `${version.id === selectedSession.latest_version_id ? "Latest captured · " : ""}${formatDateTime(version.captured_at)} · ${formatBytes(version.byte_size)}`,
                            })),
                            ...(versions.length === 0 && !requestedVersionUnavailable
                              ? [{ value: "", label: "No captured versions" }]
                              : []),
                          ]
                    }
                  />
                  {selectedVersion !== null && (
                    <Text size="xs" c="dimmed" mt={4} style={{ overflowWrap: "anywhere" }}>
                      {selectedVersion.complete ? "All JSONL lines parsed" : "Some JSONL lines did not parse"} ·{" "}
                      {selectedVersion.parse_issue_count} parse issues · SHA-256 {selectedVersion.sha256.slice(0, 12)}
                    </Text>
                  )}
                </Box>
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
              ) : selectedVersion === null && !loadingVersions ? (
                <Center h={180}>
                  <Stack align="center" gap="xs" maw={480}>
                    <Text size="sm" fw={600}>
                      {transcriptStatusLabel(selectedSession)}
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
                <ScrollArea className="session-browser-events" type="auto">
                  <Stack gap="sm" pb="md">
                    {events.map((record) => (
                      <EventRow key={`${record.ordinal}`} record={record} />
                    ))}
                    {events.length === 0 && !loadingEvents && (
                      <Text size="sm" c="dimmed">
                        This captured version contains no JSONL records.
                      </Text>
                    )}
                    {eventCursor !== null && (
                      <Button variant="default" loading={loadingMoreEvents} onClick={() => void loadMoreEvents()}>
                        Load more records
                      </Button>
                    )}
                  </Stack>
                </ScrollArea>
              )}
            </Stack>
          )}
        </Paper>
      </Box>
    </Stack>
  );
}

function SessionRow({
  session,
  machineName,
  selected,
  to,
  onClick,
}: {
  session: SessionSummary;
  machineName: string;
  selected: boolean;
  to: string;
  onClick: () => void;
}): JSX.Element {
  return (
    <Button
      component={Link}
      to={to}
      onClick={onClick}
      variant={selected ? "light" : "subtle"}
      color={selected ? "blue" : "gray"}
      fullWidth
      justify="flex-start"
      aria-current={selected ? "page" : undefined}
      styles={{
        root: { height: "auto", padding: "8px", textAlign: "left" },
        label: { display: "block", width: "100%" },
      }}
    >
      <Stack gap={2} style={{ width: "100%", alignItems: "stretch" }}>
        <Group justify="space-between" gap="xs" wrap="nowrap">
          <Text size="xs" fw={600} truncate>
            {machineName}
          </Text>
          <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
            {formatDateTime(session.last_activity_at)}
          </Text>
        </Group>
        <Text size="sm" fw={500} lineClamp={2} style={{ whiteSpace: "normal" }}>
          {session.title || "Untitled session"}
        </Text>
        <Group justify="space-between" gap="xs">
          <Text size="xs" c="dimmed">
            {harnessLabel(session.harness)}
          </Text>
          <Group gap={6} wrap="wrap" justify="flex-end">
            <Badge size="xs" color={transcriptStatusColor(session)} variant="light">
              {transcriptStatusLabel(session)}
            </Badge>
            <Text size="xs" c="dimmed">
              {session.version_count} {session.version_count === 1 ? "version" : "versions"}
            </Text>
          </Group>
        </Group>
      </Stack>
    </Button>
  );
}

function EventRow({ record }: { record: EventRecord }): JSX.Element {
  return (
    <Paper withBorder radius="sm" p="sm" data-event-ordinal={record.ordinal}>
      <Stack gap="xs">
        <Group justify="space-between" align="start" gap="xs">
          <Group gap="xs">
            <Badge variant="light">{recordLabel(record)}</Badge>
            <Text size="xs" c="dimmed">
              Line {record.ordinal}
            </Text>
            {record.parse_error != null && (
              <Badge color="orange" variant="light">
                Malformed JSONL
              </Badge>
            )}
          </Group>
          <Text size="xs" c="dimmed">
            {formatDateTime(record.timestamp)}
          </Text>
        </Group>
        <RecordContent record={record} />
        <details>
          <summary style={{ cursor: "pointer", fontSize: "var(--mantine-font-size-xs)" }}>Raw JSONL record</summary>
          <Box
            component="pre"
            mt="xs"
            p="xs"
            style={{
              background: "var(--mantine-color-default-hover)",
              borderRadius: "var(--mantine-radius-sm)",
              maxHeight: 360,
              overflow: "auto",
              whiteSpace: "pre-wrap",
              overflowWrap: "anywhere",
              fontSize: "var(--mantine-font-size-xs)",
            }}
          >
            {rawRecordText(record.raw_base64)}
          </Box>
          <details>
            <summary style={{ cursor: "pointer", fontSize: "var(--mantine-font-size-xs)" }}>
              Exact line bytes (base64)
            </summary>
            <Box
              component="pre"
              mt="xs"
              style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: "var(--mantine-font-size-xs)" }}
            >
              {record.raw_base64}
            </Box>
          </details>
        </details>
      </Stack>
    </Paper>
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

function formatDateTime(value: string | null): string {
  if (value === null) return "Unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
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
