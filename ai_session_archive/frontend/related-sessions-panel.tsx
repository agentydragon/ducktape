import { useEffect, useMemo, useState, type JSX } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Badge, Button, Group, Loader, Paper, Popover, Stack, Text } from "@mantine/core";
import { Link } from "react-router";

import { errorMessage, uniqueById, type SessionBrowserApi } from "./api";
import type { SessionSummary } from "./openapi_schema";
import { formatDateTime } from "./session-viewer-formatting";

export function RelatedSessionsPanel({
  api,
  session,
  layoutWidth,
  pathForSession,
  onNavigate,
}: {
  api: SessionBrowserApi;
  session: SessionSummary;
  layoutWidth: number;
  pathForSession: (sessionId: string) => string;
  onNavigate: () => void;
}): JSX.Element | null {
  const [opened, setOpened] = useState(false);
  const parentSessionId = session.parent_session_id ?? null;
  const parentLinkKind = session.parent_link_kind ?? null;
  const parentNativeSessionId = session.parent_native_session_id ?? null;
  const parentQuery = useQuery({
    queryKey: ["ai-session-archive", "session", parentSessionId],
    queryFn: ({ signal }) => {
      if (parentSessionId === null) throw new Error("A parent session ID is required.");
      return api.getSession(parentSessionId, signal);
    },
    enabled: parentSessionId !== null,
  });
  const childQuery = useInfiniteQuery({
    queryKey: ["ai-session-archive", "children", session.id],
    queryFn: ({ pageParam, signal }) =>
      api.listSessions(
        {
          parentSessionId: session.id,
          ...(pageParam === null ? {} : { cursor: pageParam }),
          limit: 50,
        },
        signal
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const childPages = childQuery.data?.pages ?? [];
  const childSessions = useMemo(() => uniqueById(childPages.flatMap((page) => page.data)), [childPages]);
  const fetchedParentSession = parentQuery.data;
  const parent = {
    session:
      fetchedParentSession !== undefined && fetchedParentSession.id === parentSessionId ? fetchedParentSession : null,
    loading: parentSessionId !== null && parentQuery.isLoading,
    error: parentQuery.error === null ? null : errorMessage(parentQuery.error),
  };
  const children = {
    items: childSessions,
    loading: childQuery.isLoading,
    loadingNext: childQuery.isFetchingNextPage,
    error: childQuery.error === null ? null : errorMessage(childQuery.error),
    cursor: childPages.at(-1)?.next_cursor ?? null,
    hasNextPage: childQuery.hasNextPage,
    onLoadMore: () => {
      // Join an in-flight page when scroll/layout events fire before the next render.
      if (childQuery.hasNextPage) void childQuery.fetchNextPage({ cancelRefetch: false });
    },
  };

  useEffect(() => {
    setOpened(false);
  }, [session.id]);

  const hasParentRelation = parentSessionId !== null || parentLinkKind !== null;
  const hasParentMetadata =
    hasParentRelation ||
    parentNativeSessionId !== null ||
    session.parent_native_session_id_conflict === true ||
    (session.parent_native_session_id_candidates?.length ?? 0) > 0;
  const hasCapturedParentSession = parentSessionId !== null;
  const relatedSessionCount = children.items.length + Number(hasCapturedParentSession);
  const hasMoreRelatedChildren = children.hasNextPage;
  const relatedSessionCountLabel = String(relatedSessionCount) + (hasMoreRelatedChildren ? "+" : "");
  const loadingRelatedSessions = parent.loading || children.loading;
  const hasRelatedSessions =
    hasParentMetadata ||
    children.items.length > 0 ||
    loadingRelatedSessions ||
    parent.error !== null ||
    children.error !== null;
  if (!hasRelatedSessions) return null;

  const closeForNavigation = (): void => {
    setOpened(false);
    onNavigate();
  };

  return (
    <Popover
      opened={opened}
      onChange={setOpened}
      position={layoutWidth <= 760 ? "top-end" : "bottom-end"}
      offset={8}
      width="min(440px, calc(100vw - 32px))"
      withinPortal
      shadow="md"
    >
      <Popover.Target>
        <Button
          className="session-browser-related-trigger"
          variant="subtle"
          size="xs"
          onClick={() => setOpened((open) => !open)}
          aria-label={
            "Related sessions (" +
            relatedSessionCount +
            " shown" +
            (hasMoreRelatedChildren ? ", more available" : "") +
            ")"
          }
          aria-controls="session-browser-related-dropdown"
          aria-expanded={opened}
        >
          <RelatedSessionsIcon />
          <Badge className="session-browser-related-count" size="sm" variant="light" aria-hidden="true">
            {relatedSessionCountLabel}
          </Badge>
        </Button>
      </Popover.Target>
      <Popover.Dropdown className="session-browser-related-dropdown">
        <Paper component="section" id="session-browser-related-dropdown" aria-label="Related sessions">
          <Stack gap="xs">
            <Text size="sm" fw={600}>
              Related sessions
            </Text>
            {hasParentMetadata && (
              <Stack gap={4}>
                <Group gap="xs" wrap="wrap">
                  <Text size="xs" c="dimmed">
                    Parent
                  </Text>
                  {parentSessionId !== null && parent.session !== null ? (
                    <Button
                      component={Link}
                      to={pathForSession(parent.session.id)}
                      variant="subtle"
                      size="xs"
                      onClick={closeForNavigation}
                    >
                      {parent.session.title || "Untitled parent session"}
                    </Button>
                  ) : parentSessionId !== null && parent.loading ? (
                    <Loader size="xs" aria-label="Loading parent session" />
                  ) : parent.error !== null ? (
                    <Text size="xs" c="red" role="alert">
                      Could not load parent session: {parent.error}
                    </Text>
                  ) : (
                    <Text size="xs" c="dimmed">
                      Parent session could not be resolved to a captured session.
                    </Text>
                  )}
                  {parentLinkLabel(parentLinkKind) !== null && (
                    <Badge className="session-browser-relation-badge" color="orange" variant="light">
                      {parentLinkLabel(parentLinkKind)}
                    </Badge>
                  )}
                  {parentNativeSessionId !== null && (
                    <Text size="xs" c="dimmed">
                      Native parent ID: {parentNativeSessionId}
                    </Text>
                  )}
                  {session.parent_native_session_id_conflict && (
                    <Text size="xs" c="red" style={{ overflowWrap: "anywhere" }}>
                      Conflicting native parent ID candidates:{" "}
                      {(session.parent_native_session_id_candidates ?? []).join(", ") || "unavailable"}
                    </Text>
                  )}
                  {parent.session?.identity_inferred && (
                    <Text size="xs" c="orange">
                      Parent transcript is not captured; its identity is inferred.
                    </Text>
                  )}
                </Group>
              </Stack>
            )}
            {(children.items.length > 0 || children.loading || children.error !== null) && (
              <Stack gap={2}>
                <Text size="xs" c="dimmed">
                  Child sessions
                </Text>
                {children.items.length > 0 && (
                  <div
                    className="session-browser-related-children"
                    role="region"
                    aria-label="Child sessions"
                    tabIndex={0}
                  >
                    <Stack gap={2} role="list">
                      {children.items.map((child) => (
                        <Group key={child.id} gap="xs" wrap="wrap" role="listitem">
                          <Button
                            component={Link}
                            to={pathForSession(child.id)}
                            variant="subtle"
                            size="xs"
                            onClick={closeForNavigation}
                          >
                            {child.title || "Untitled child session"}
                          </Button>
                          {parentLinkLabel(child.parent_link_kind ?? null) !== null && (
                            <Badge className="session-browser-relation-badge" color="orange" variant="light">
                              {parentLinkLabel(child.parent_link_kind ?? null)}
                            </Badge>
                          )}
                          <Text size="xs" c="dimmed">
                            {formatDateTime(child.last_activity_at)}
                          </Text>
                        </Group>
                      ))}
                    </Stack>
                  </div>
                )}
                {children.loading && <Loader size="xs" aria-label="Loading child sessions" />}
                {children.error !== null && (
                  <Text size="xs" c="red" role="alert">
                    Could not load child sessions: {children.error}
                  </Text>
                )}
                {children.cursor !== null && (
                  <Button variant="default" size="xs" loading={children.loadingNext} onClick={children.onLoadMore}>
                    Load more child sessions
                  </Button>
                )}
              </Stack>
            )}
          </Stack>
        </Paper>
      </Popover.Dropdown>
    </Popover>
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
      return "Parent link source: " + kind.replaceAll("_", " ");
  }
}

function RelatedSessionsIcon(): JSX.Element {
  return (
    <svg aria-hidden="true" focusable="false" width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M6 7v10m0 0h5a7 7 0 0 0 7-7V7" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="6" cy="5" r="2" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="6" cy="19" r="2" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="18" cy="5" r="2" stroke="currentColor" strokeWidth="1.8" />
    </svg>
  );
}
