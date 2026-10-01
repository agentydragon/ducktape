import { useCallback, useEffect, useMemo, useState, type JSX } from "react";

import {
  ApiError,
  listSessionEvents,
  listSessions,
  watchSessions,
  type SessionEvent,
  type SessionSummary,
} from "./api";

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

function readableText(value: unknown, depth = 0): string | null {
  if (typeof value === "string") return value.trim() === "" ? null : value;
  if (depth >= 4) return null;
  if (Array.isArray(value)) {
    const parts = value.map((part) => readableText(part, depth + 1)).filter((part): part is string => part !== null);
    return parts.length === 0 ? null : parts.join("\n");
  }
  if (typeof value === "object" && value !== null) {
    const record = value as Record<string, unknown>;
    if (typeof record.text === "string") return readableText(record.text, depth + 1);
    if (record.content !== undefined) return readableText(record.content, depth + 1);
    if (record.message !== undefined) return readableText(record.message, depth + 1);
  }
  return null;
}

function eventText(event: SessionEvent): string | null {
  const payload = event.payload as Record<string, unknown>;
  return readableText(payload.text) ?? readableText(payload.content) ?? readableText(payload.message);
}

function eventTime(event: SessionEvent): string {
  const date = new Date(event.created_at);
  return Number.isNaN(date.getTime()) ? event.created_at : date.toLocaleString();
}

function StatusDot({ status }: { status: string }): JSX.Element {
  const color = status === "active" || status === "paused" ? ` status-dot-${status}` : "";
  return <span className={`status-dot${color}`} role="img" aria-label={`Status: ${status}`} title={status} />;
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
    <li>
      <button
        type="button"
        className={`session-item${selected ? " selected" : ""}`}
        aria-pressed={selected}
        onClick={onSelect}
      >
        <span className="session-item-title">{session.title || "Untitled session"}</span>
        <span className="session-item-meta">
          <StatusDot status={session.status} />
          <time dateTime={session.updated_at}>{new Date(session.updated_at).toLocaleDateString()}</time>
        </span>
        <span className="session-item-subtitle">{sessionSubtitle(session)}</span>
      </button>
    </li>
  );
}

function EventCard({ event }: { event: SessionEvent }): JSX.Element {
  const text = eventText(event);
  return (
    <article className="transcript-event">
      <header>
        <strong>{event.event_type}</strong>
        <span>#{event.sequence_num}</span>
        <time dateTime={event.created_at}>{eventTime(event)}</time>
      </header>
      {text !== null && <p className="transcript-text">{text}</p>}
      <details>
        <summary>{text === null ? "Event payload" : "Raw event payload"}</summary>
        <pre>{JSON.stringify(event.payload, null, 2)}</pre>
      </details>
    </article>
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
    <section className="session-viewer" aria-labelledby="session-viewer-title">
      <header className="session-viewer-heading">
        <div>
          <h2 id="session-viewer-title">Sessions</h2>
          <p className="hint">Read-only view of the synced Claude Code session history.</p>
        </div>
        {resumeToken !== null && (
          <span role="status" className={`watch-state watch-${watchStatus}`}>
            {watchStatus === "connected"
              ? "Live updates on"
              : watchStatus === "connecting"
                ? "Connecting…"
                : "Reconnecting…"}
          </span>
        )}
        <button type="button" className="secondary" onClick={() => setRefreshCount((count) => count + 1)}>
          Refresh
        </button>
      </header>
      <div className="session-tools">
        <label className="search-label">
          <span>Search loaded sessions</span>
          <input type="search" value={search} onChange={(event) => setSearch(event.currentTarget.value)} />
        </label>
        <label className="filter-label">
          <span>Status</span>
          <select value={filter} onChange={(event) => setFilter(event.currentTarget.value as StatusFilter)}>
            <option value="all">All sessions</option>
            <option value="active">Active</option>
            <option value="paused">Paused</option>
            <option value="archived">Archived</option>
          </select>
        </label>
      </div>
      {sessionError !== null && (
        <p role="alert" className="error viewer-error">
          {sessionError}
        </p>
      )}
      <div className="session-browser">
        <aside className="session-list" aria-label="Session list">
          {loadingSessions ? (
            <p className="empty-state">Loading sessions…</p>
          ) : visibleSessions.length === 0 ? (
            <p className="empty-state">
              {sessions.length === 0 ? "No sessions in the sync yet." : "No matching sessions."}
            </p>
          ) : (
            <ul>
              {visibleSessions.map((session) => (
                <SessionRow
                  key={session.id}
                  session={session}
                  selected={session.id === selectedId}
                  onSelect={() => setSelectedId(session.id)}
                />
              ))}
            </ul>
          )}
          {nextSessionCursor !== null && (
            <button
              type="button"
              className="secondary load-more"
              disabled={loadingMoreSessions}
              onClick={() => void loadMoreSessions()}
            >
              {loadingMoreSessions ? "Loading…" : "Load more sessions"}
            </button>
          )}
        </aside>
        <div className="transcript-panel" aria-busy={loadingEvents}>
          {selectedSession === null ? (
            <p className="empty-state">Select a session to view its transcript.</p>
          ) : (
            <>
              <header className="transcript-heading">
                <div>
                  <h3>{selectedSession.title || "Untitled session"}</h3>
                  <p className="hint">{sessionSubtitle(selectedSession)}</p>
                </div>
                <StatusDot status={selectedSession.status} />
              </header>
              {eventError !== null && (
                <p role="alert" className="error">
                  {eventError}
                </p>
              )}
              {loadingEvents ? (
                <p className="empty-state">Loading transcript…</p>
              ) : events.length === 0 ? (
                <p className="empty-state">No events are stored for this session yet.</p>
              ) : (
                <>
                  {events.map((event) => (
                    <EventCard key={`${event.sequence_num}-${event.event_id}`} event={event} />
                  ))}
                  {hasMoreEvents && (
                    <button
                      type="button"
                      className="secondary load-more"
                      disabled={loadingMoreEvents}
                      onClick={() => void loadMoreEvents()}
                    >
                      {loadingMoreEvents ? "Loading…" : "Load more events"}
                    </button>
                  )}
                </>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
