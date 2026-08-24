import { Badge, Box, Button, Code, Divider, Group, Loader, Paper, Stack, Text, Title } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import {
  closeSession,
  createConversation,
  displayableError,
  fetchConversations,
  type ConversationItem,
  type Conversation,
  type ConversationCursor,
  type ConversationSession,
  type ConversationSummary,
} from "../client";
import { useCoalescedRefresh } from "../coalesced_refresh";
import { changedSessionId, useConsoleEvents } from "../console_events";
import { useFollowedConversation } from "./conversation_follow";
import { conversationPath, CONVERSATIONS_PATH, navigateToConsolePath, sessionFramesPath } from "../routing";
import { bootstrapNarration, type BootstrapNarration } from "./bootstrap_narration";
import { isNearChatBottom } from "./chat_scroll";
import { ToolCallView } from "./tool_call";
import { ConversationComposer } from "./conversation_composer";
import { conversationTimeline, type ConversationTurn } from "./conversation_timeline";
import { Markdown } from "./markdown";
import { SandboxProvisioning } from "./sandbox_provisioning";

/** A session that has ended takes no more prompts, so it gets no composer. */
const SETTLED = new Set<ConversationSession["status"]>(["closing", "closed", "failed"]);

function openConversation(conversationId: string): void {
  navigateToConsolePath(conversationPath(conversationId));
}

function backToConversations(): void {
  navigateToConsolePath(CONVERSATIONS_PATH);
}

function statusColor(status: ConversationSession["status"]): string {
  if (status === "ready") return "teal";
  if (status === "responding" || status === "provisioning") return "blue";
  if (status === "failed") return "red";
  return "gray";
}

function timestamp(value: string): string {
  return `${value.slice(0, 16).replace("T", " ")} UTC`;
}

/** The channels holding a copy of this conversation, or that none do.
 *
 * Plural on purpose: a conversation is held by however many channels have attached to it. The
 * browser reading it is not one of them — a tab keeps no copy, so it has no attachment.
 */
function Attachments({ attachments }: { attachments: ConversationSummary["attachments"] }) {
  if (attachments.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No channel attached
      </Text>
    );
  }
  return (
    <Group gap={6} wrap="wrap">
      {attachments.map((attachment) => (
        <Badge key={`${attachment.surface}:${attachment.address}`} size="sm" variant="outline">
          {attachment.surface}: {attachment.address}
        </Badge>
      ))}
    </Group>
  );
}

/** Where one exchange began, drawn across the transcript.
 *
 * No start time: the boundary's own position already says when, and the narrow viewport has no
 * room for a wall-clock value that would only repeat it.
 */
function TurnBoundary({ turn, number }: { turn: ConversationTurn; number: number }) {
  return (
    <Divider
      className="haku-conversation-turn-boundary"
      labelPosition="center"
      label={
        <Group gap={6} justify="center">
          <Text fw={600} size="xs">
            Turn {number}
          </Text>
          <Badge size="xs" color={turn.outcome === "failed" ? "red" : "teal"} variant="light">
            {turn.outcome ?? "running"}
          </Badge>
        </Group>
      }
    />
  );
}

/** The sandbox's own account of coming up, at the head of the transcript where it happened.
 *
 * Narration is recorded before the CLI produces anything, and a session that never got past setup
 * has nothing below it. Open while that is still the case (`bootstrapNarration` decides), collapsed
 * to a summary line once the transcript is what the operator came to read — keeping the last line
 * visible, since a bootstrap that ended badly says so on its final line.
 */
function BootstrapNarrationPanel({ narration, starting }: { narration: BootstrapNarration; starting: boolean }) {
  const [expanded, setExpanded] = useState(narration.startsExpanded);
  return (
    <Paper withBorder p="sm" className="haku-conversation-narration">
      <Group justify="space-between" align="center" wrap="nowrap" gap="xs">
        <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
          {starting && <Loader size="xs" />}
          <Text fw={600} size="xs">
            Sandbox setup
          </Text>
          <Text c="dimmed" size="xs">
            {narration.lines.length === 1 ? "1 line" : `${narration.lines.length} lines`}
          </Text>
        </Group>
        <Button variant="subtle" size="compact-xs" onClick={() => setExpanded(!expanded)}>
          {expanded ? "Hide" : "Show"}
        </Button>
      </Group>
      {expanded ? (
        <Code block className="haku-conversation-narration-log" mt="xs">
          {narration.lines.map((line) => line.text).join("\n")}
        </Code>
      ) : (
        <Text c="dimmed" size="xs" mt={4} lineClamp={1}>
          {narration.lines[narration.lines.length - 1].text}
        </Text>
      )}
    </Paper>
  );
}

/** Who produced an item, in the words the transcript shows. */
const SPOKE_BY: Record<ConversationItem["item_type"], string> = {
  prompt: "You",
  message: "Claude",
  reasoning: "Claude thought",
  tool_call: "Claude",
};

/** One item of the transcript. A tool call is a sibling of the message rather than a field on it,
 * which is why it renders here rather than inside one. */
function ItemView({ item }: { item: ConversationItem }) {
  if (item.item_type === "tool_call") return <ToolCallView item={item} />;
  return (
    <Paper withBorder p="sm" className={`haku-chat-message haku-chat-message-${item.item_type}`}>
      <Group justify="space-between" align="center" mb={4}>
        <Text fw={600} size="xs">
          {SPOKE_BY[item.item_type]}
        </Text>
        {item.status !== "complete" && (
          <Badge size="xs" variant="light" color={item.status === "failed" ? "red" : "blue"}>
            {item.status}
          </Badge>
        )}
      </Group>
      <Markdown source={item.text.trim() || (item.status === "open" ? "…" : "")} className="haku-chat-markdown" />
      {!item.text.trim() && item.status === "complete" && (
        <Text c="dimmed" size="xs">
          {item.item_type === "reasoning" && item.disclosure === "withheld"
            ? "The model thought, and none of it was disclosed."
            : "Nothing was captured for this."}
        </Text>
      )}
    </Paper>
  );
}

/** Every conversation this operator has, newest activity first, and the button that starts one.
 *
 * **Paged by keyset**, because a conversation never ends: this list only grows, and only at its
 * top, so an offset would step over a row or repeat one every time something moved mid-walk. Pages
 * already loaded are kept and appended to; a live event refreshes only the newest page, the way the
 * tool-call history does.
 */
function ConversationListPage() {
  // Null until the first read lands: an empty inventory and an unread one look the same
  // otherwise, and the two want different things on screen.
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [nextCursor, setNextCursor] = useState<ConversationCursor | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);

  const { refresh } = useCoalescedRefresh(async () => {
    try {
      const page = await fetchConversations();
      // The newest page replaces its own rows and keeps whatever older pages were loaded below it:
      // refetching everything would cost the whole walk on each of a turn's invalidations.
      setConversations((loaded) => {
        const newest = new Set(page.conversations.map((conversation) => conversation.conversation_id));
        return [
          ...page.conversations,
          ...(loaded ?? []).filter((conversation) => !newest.has(conversation.conversation_id)),
        ];
      });
      setNextCursor((cursor) => cursor ?? page.next_cursor);
      setError(null);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  });

  // The initial read, a re-read when any session changes, and one more on every reconnect. A row
  // carries `item_count` and `last_activity_at`, so it goes stale for the same reason a
  // transcript does, and the event does not say which row moved.
  useConsoleEvents((event) => {
    if (event.event_type === "sync" || changedSessionId(event) !== null) refresh();
  });

  async function loadOlder() {
    if (nextCursor === null) return;
    setLoadingOlder(true);
    try {
      const page = await fetchConversations(nextCursor);
      setConversations((loaded) => [...(loaded ?? []), ...page.conversations]);
      setNextCursor(page.next_cursor);
      setError(null);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    } finally {
      setLoadingOlder(false);
    }
  }

  async function start() {
    setStarting(true);
    setError(null);
    try {
      openConversation((await createConversation()).conversation_id);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    } finally {
      setStarting(false);
    }
  }

  return (
    <section className="haku-page" aria-label="Conversations">
      <header className="haku-page-header">
        <div className="haku-page-bar">
          <div>
            <Title order={1}>Conversations</Title>
            <Text c="dimmed" size="sm">
              Every thread you have with Haku, wherever it is being held.
            </Text>
          </div>
          <Button onClick={() => void start()} loading={starting}>
            New conversation
          </Button>
        </div>
      </header>
      <div className="haku-page-scroll">
        <div className="haku-page-list">
          {error && (
            <Paper withBorder p="sm">
              <Text c="red" size="sm">
                {error}
              </Text>
            </Paper>
          )}
          {conversations === null ? (
            <Paper withBorder p="xl">
              <Stack align="center" gap="xs">
                <Loader size="sm" />
                <Text c="dimmed" size="sm">
                  Loading conversations…
                </Text>
              </Stack>
            </Paper>
          ) : conversations.length === 0 ? (
            <Paper withBorder p="xl">
              <Text c="dimmed" size="sm">
                No conversations yet.
              </Text>
            </Paper>
          ) : (
            conversations.map((conversation) => (
              <button
                key={conversation.conversation_id}
                type="button"
                className="haku-conversation-list-item"
                onClick={() => openConversation(conversation.conversation_id)}
              >
                <Group justify="space-between" align="flex-start" wrap="nowrap">
                  <Box className="haku-conversation-list-item-main">
                    <Group gap="xs" mb={4}>
                      <Attachments attachments={conversation.attachments} />
                      {conversation.live_session ? (
                        <Badge size="sm" color={statusColor(conversation.live_session.status)} variant="light">
                          {conversation.live_session.status}
                        </Badge>
                      ) : conversation.last_session_status === "failed" ? (
                        // A failed session is not live, so without this branch the row would read
                        // like any idle thread.
                        <Badge size="sm" color="red" variant="light">
                          failed
                        </Badge>
                      ) : (
                        <Badge size="sm" color="gray" variant="light">
                          no live session
                        </Badge>
                      )}
                    </Group>
                    <Text size="xs" c="dimmed" mt={4}>
                      {conversation.item_count} items · active {timestamp(conversation.last_activity_at)}
                    </Text>
                  </Box>
                  <Text size="sm" c="dimmed" className="haku-conversation-open">
                    Open →
                  </Text>
                </Group>
              </button>
            ))
          )}
          {nextCursor !== null && (
            <Button variant="subtle" onClick={() => void loadOlder()} loading={loadingOlder}>
              Load older conversations
            </Button>
          )}
        </div>
      </div>
    </section>
  );
}

/** The sessions this conversation ran before the current one.
 *
 * A conversation outlives its sessions, so a thread whose sandbox died has more than one. Their
 * transcripts are not merged into the one above — that is the subscription's job — so what they get
 * here is the link that keeps their frame log reachable.
 */
function EarlierSessions({ sessions }: { sessions: Conversation["earlier_sessions"] }) {
  return (
    <Paper withBorder p="sm">
      <Text fw={600} size="xs" mb={4}>
        {sessions.length === 1 ? "1 earlier session" : `${sessions.length} earlier sessions`}
      </Text>
      <Stack gap={4}>
        {sessions.map((session) => (
          <Group key={session.session_id} gap="xs" wrap="nowrap">
            <Badge size="xs" color={statusColor(session.status)} variant="light">
              {session.status}
            </Badge>
            <Text size="xs" c="dimmed">
              started {timestamp(session.created_at)}
            </Text>
            <Button
              variant="subtle"
              size="compact-xs"
              onClick={() => navigateToConsolePath(sessionFramesPath(session.session_id))}
            >
              Raw frames
            </Button>
          </Group>
        ))}
      </Stack>
    </Paper>
  );
}

function ConversationDetailPage({ conversationId }: { conversationId: string }) {
  const [closing, setClosing] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const transcriptScrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);

  // One socket, carrying this conversation's state and then every change to it. Nothing here
  // refetches: the page holds a position rather than a timer, and what it shows moves when the
  // conversation does — including a session replaced under it, which arrives as an ordinary update.
  const { conversation, status: liveStatus, error: followError } = useFollowedConversation(conversationId);
  const error = actionError ?? followError;

  useEffect(() => {
    setActionError(null);
    stickToBottomRef.current = true;
  }, [conversationId]);

  // A transcript opens on its newest message: the operator came to read what just happened, not
  // the first thing said. The layout settles a frame after the transcript renders, so the scroll
  // waits for it.
  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const viewport = transcriptScrollRef.current;
      if (viewport) viewport.scrollTop = viewport.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [conversation]);

  if (error) {
    return (
      <section className="haku-page" aria-label="Conversation">
        <header className="haku-page-header">
          <div className="haku-page-bar">
            <Button variant="subtle" onClick={backToConversations}>
              ← Conversations
            </Button>
          </div>
        </header>
        <div className="haku-page-list">
          <Text c="red">{error}</Text>
        </div>
      </section>
    );
  }

  if (!conversation) {
    return (
      <section className="haku-page" aria-label="Conversation">
        <div className="haku-page-list">
          <Loader size="sm" />
        </div>
      </section>
    );
  }

  const { session } = conversation;
  const narration = bootstrapNarration(session);
  const timeline = conversationTimeline(session.items, session.turns);

  const close = async (sessionId: string) => {
    setClosing(true);
    try {
      // No refetch afterwards: closing writes rows, and the follow socket carries what they became.
      await closeSession(sessionId);
    } catch (reason: unknown) {
      setActionError(displayableError(reason));
    } finally {
      setClosing(false);
    }
  };

  return (
    <section className="haku-page" aria-label="Conversation">
      <header className="haku-page-header">
        <div className="haku-page-bar haku-conversation-detail-header">
          <div>
            <Button variant="subtle" size="compact-sm" onClick={backToConversations}>
              ← Conversations
            </Button>
            <Title order={1}>Conversation</Title>
            <Attachments attachments={conversation.attachments} />
            <Text c="dimmed" size="sm">
              started {timestamp(conversation.created_at)}
            </Text>
          </div>
          <Group gap="xs" wrap="nowrap" align="center">
            {/* The transcript is a lossy projection of the frame log, so an operator reading one
                that looks wrong needs the record it was projected from — one click away. */}
            <Button
              variant="light"
              size="compact-sm"
              onClick={() => navigateToConsolePath(sessionFramesPath(session.session_id))}
            >
              Raw frames
            </Button>
            {!SETTLED.has(session.status) && (
              <Button
                variant="light"
                color="red"
                size="compact-sm"
                onClick={() => void close(session.session_id)}
                loading={closing}
              >
                Close session
              </Button>
            )}
            <Badge color={statusColor(session.status)} variant="light">
              {session.status}
            </Badge>
            {/* A dead socket means this transcript has stopped moving, and nothing else on the page
                would say so: there is no timer behind it to paper over the gap. */}
            {liveStatus === "offline" && (
              <Badge color="orange" variant="light">
                reconnecting
              </Badge>
            )}
          </Group>
        </div>
      </header>
      <div className="haku-conversation-detail-body">
        <div
          ref={transcriptScrollRef}
          className="haku-conversation-transcript-scroll"
          onScroll={(event) => {
            stickToBottomRef.current = isNearChatBottom(event.currentTarget);
          }}
        >
          <div className="haku-page-list haku-chat-messages">
            {conversation.earlier_sessions.length > 0 && <EarlierSessions sessions={conversation.earlier_sessions} />}
            {session.provisioning && <SandboxProvisioning provisioning={session.provisioning} />}
            {narration && (
              <BootstrapNarrationPanel narration={narration} starting={session.status === "provisioning"} />
            )}
            {timeline.length === 0 && !narration && !session.provisioning && (
              <Text c="dimmed" size="sm">
                Nothing was recorded for this session.
              </Text>
            )}
            {timeline.map((entry) =>
              entry.kind === "item" ? (
                <ItemView key={entry.item.item_id} item={entry.item} />
              ) : (
                <TurnBoundary key={entry.turn.turn_id} turn={entry.turn} number={entry.number} />
              )
            )}
          </div>
        </div>
        {!SETTLED.has(session.status) && (
          <ConversationComposer
            conversationId={conversation.conversation_id}
            sessionId={session.session_id}
            status={session.status}
            onSent={() => {
              // The accepted prompt is the operator's own, so the transcript follows it down
              // rather than holding whatever they had scrolled back to. The prompt row itself
              // arrives on the follow socket, like every other thing that happens here.
              stickToBottomRef.current = true;
            }}
          />
        )}
      </div>
    </section>
  );
}

export function ConversationsPage({ conversationId }: { conversationId: string | null }) {
  return conversationId === null ? (
    <ConversationListPage />
  ) : (
    <ConversationDetailPage conversationId={conversationId} />
  );
}
