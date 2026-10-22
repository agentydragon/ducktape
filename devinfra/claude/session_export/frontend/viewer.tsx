import { useEffect, useMemo, useRef, useState, type JSX } from "react";
import { Alert, Badge, Button, Center, Drawer, Group, Paper, Stack, Text } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";

import { SessionList } from "./session-list";
import { sessionMatchesFilter, type StatusFilter } from "./session-info";
import { TranscriptView } from "./transcript-view";
import { useSessionDirectory } from "./use-session-directory";
import { useTranscriptHistory } from "./use-transcript-history";

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
  const [selectedId, setSelectedId] = useState<string | null>(null);

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

  const transcriptHistory = useTranscriptHistory(selectedId);
  const directory = useSessionDirectory({
    filter,
    selectedId,
    setSelectedId,
    refreshSelectedEvents: transcriptHistory.refreshSelectedEvents,
  });

  const visibleSessions = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const matchingStatus = directory.sessions.filter((session) => sessionMatchesFilter(session, filter));
    return needle === ""
      ? matchingStatus
      : matchingStatus.filter((session) => JSON.stringify(session).toLowerCase().includes(needle));
  }, [filter, search, directory.sessions]);

  const selectedSession =
    directory.sessions.find((session) => session.id === selectedId && sessionMatchesFilter(session, filter)) ?? null;
  const watchLabel =
    directory.watchStatus === "connected"
      ? "Live updates on"
      : directory.watchStatus === "connecting"
        ? "Connecting…"
        : "Reconnecting…";

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
      sessions={directory.sessions}
      visibleSessions={visibleSessions}
      selectedId={selectedId}
      loadingSessions={directory.loadingSessions}
      nextSessionCursor={directory.nextSessionCursor}
      loadingMoreSessions={directory.loadingMoreSessions}
      search={search}
      filter={filter}
      onSearchChange={setSearch}
      onFilterChange={setFilter}
      onSelect={(sessionId) => {
        setSelectedId(sessionId);
        if (closeDrawerOnSelect) setMobileDrawerOpen(false);
      }}
      onLoadMore={() => void directory.loadMoreSessions()}
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
              {directory.resumeToken !== null && (
                <Badge role="status" variant="dot" color={directory.watchStatus === "connected" ? "green" : "yellow"}>
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

          {directory.sessionError !== null && (
            <Alert color="red" title="Could not load sessions">
              {directory.sessionError}
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
                <TranscriptView
                  selectedSession={selectedSession}
                  selectedId={selectedSession.id}
                  events={transcriptHistory.events}
                  loadingEvents={transcriptHistory.loadingEvents}
                  eventError={transcriptHistory.eventError}
                  hasMoreEvents={transcriptHistory.hasMoreEvents}
                  loadingMoreEvents={transcriptHistory.loadingMoreEvents}
                  showRawEvents={showRawEvents}
                  onToggleRawEvents={() => setShowRawEvents((value) => !value)}
                  setEventViewport={transcriptHistory.setEventViewport}
                  loadMoreEvents={() => void transcriptHistory.loadMoreEvents()}
                />
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
