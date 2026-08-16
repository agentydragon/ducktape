import { Badge, Box, Button, Code, Divider, Group, Loader, Paper, Stack, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  displayableError,
  fetchConversation,
  fetchConversations,
  type ClaudeChatMessage,
  type ConversationSession,
  type ConversationSessionSummary,
} from "../client";
import { useCoalescedRefresh } from "../coalesced_refresh";
import { changedSessionId, useConsoleEvents } from "../console_events";
import { conversationPath, CONVERSATIONS_PATH, navigateToConsolePath, sessionFramesPath } from "../routing";
import { bootstrapNarration, type BootstrapNarration } from "./bootstrap_narration";
import { isNearChatBottom } from "./chat_scroll";
import { ToolCallView } from "./tool_call";
import { conversationTimeline, type ConversationTurn } from "./conversation_timeline";
import { Markdown } from "./markdown";

function openConversation(sessionId: string): void {
  navigateToConsolePath(conversationPath(sessionId));
}

function backToConversations(): void {
  navigateToConsolePath(CONVERSATIONS_PATH);
}

function statusColor(status: ConversationSessionSummary["status"]): string {
  if (status === "ready") return "teal";
  if (status === "responding" || status === "provisioning") return "blue";
  if (status === "failed") return "red";
  return "gray";
}

function timestamp(value: string): string {
  return `${value.slice(0, 16).replace("T", " ")} UTC`;
}

function surfaceLabel(summary: { surface: ConversationSessionSummary["surface"]; room_id: string | null }): string {
  if (summary.surface === "matrix") return "Matrix";
  if (summary.surface === "spa") return "Console chat";
  return "Conversation";
}

/** Where one exchange began, and what it cost, drawn across the transcript.
 *
 * No start time: the boundary's own position already says when, and the narrow viewport has no
 * room for a wall-clock value that would only repeat it.
 */
function TurnBoundary({ turn, number }: { turn: ConversationTurn; number: number }) {
  const facts = [
    turn.usage?.duration_ms == null ? null : `${(turn.usage.duration_ms / 1000).toFixed(1)}s`,
    turn.usage?.cost_usd == null ? null : `$${turn.usage.cost_usd.toFixed(4)}`,
  ].filter((fact) => fact !== null);
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
          {facts.length > 0 && (
            <Text size="xs" c="dimmed">
              {facts.join(" · ")}
            </Text>
          )}
        </Group>
      }
    />
  );
}

/** The sandbox's own account of coming up, at the head of the transcript where it happened.
 *
 * It sits first because it is first: narration is recorded before the CLI produces anything, and
 * a session that never got past setup has nothing below it. Open while that is still the case
 * (`bootstrapNarration` decides), collapsed to a summary line once the transcript is what the
 * operator came to read — with the last line kept visible there, since a bootstrap that ended
 * badly says so on its final line.
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

function MessageView({ message }: { message: ClaudeChatMessage }) {
  return (
    <Paper withBorder p="sm" className={`haku-chat-message haku-chat-message-${message.role}`}>
      <Group justify="space-between" align="center" mb={4}>
        <Text fw={600} size="xs">
          {message.role === "user" ? "You" : "Claude"}
        </Text>
        {message.status !== "complete" && (
          <Badge size="xs" variant="light" color={message.status === "failed" ? "red" : "blue"}>
            {message.status}
          </Badge>
        )}
      </Group>
      {message.tool_calls.length > 0 && (
        <Stack gap="xs" mb="sm">
          {message.tool_calls.map((toolCall) => (
            <ToolCallView key={toolCall.call_id} toolCall={toolCall} />
          ))}
        </Stack>
      )}
      <Markdown
        source={message.content.trim() || (message.status === "streaming" ? "…" : "")}
        className="haku-chat-markdown"
      />
      {!message.content.trim() && message.tool_calls.length === 0 && message.status === "complete" && (
        <Text c="dimmed" size="xs">
          No assistant text was captured.
        </Text>
      )}
      {message.error && (
        <Text c="red" size="xs" mt="xs">
          {message.error}
        </Text>
      )}
    </Paper>
  );
}

function ConversationListPage() {
  // Null until the first read lands: an empty inventory and an unread one look the same
  // otherwise, and the two want different things on screen.
  const [conversations, setConversations] = useState<ConversationSessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { refresh } = useCoalescedRefresh(async () => {
    try {
      setConversations(await fetchConversations());
      setError(null);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    }
  });

  // The initial read, a re-read when any session changes, and one more on every reconnect. The
  // inventory shows `message_count` and `updated_at`, so it goes stale for exactly the reason a
  // transcript does — and it is not told which of its rows moved, only that one did.
  useConsoleEvents((event) => {
    if (event.event_type === "sync" || changedSessionId(event) !== null) refresh();
  });

  return (
    <section className="haku-page" aria-label="Conversations">
      <header className="haku-page-header">
        <div className="haku-page-bar">
          <div>
            <Title order={1}>Conversations</Title>
            <Text c="dimmed" size="sm">
              Sessions handled by your linked agents and Console runtimes.
            </Text>
          </div>
          <Badge variant="light" className="haku-conversation-count">
            {conversations?.length ?? 0} sessions
          </Badge>
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
                key={conversation.session_id}
                type="button"
                className="haku-conversation-list-item"
                onClick={() => openConversation(conversation.session_id)}
              >
                <Group justify="space-between" align="flex-start" wrap="nowrap">
                  <Box className="haku-conversation-list-item-main">
                    <Group gap="xs" mb={4}>
                      <Text fw={600}>{surfaceLabel(conversation)}</Text>
                      <Badge size="sm" color={statusColor(conversation.status)} variant="light">
                        {conversation.status}
                      </Badge>
                    </Group>
                    <Text size="sm" c="dimmed" className="haku-conversation-room">
                      {conversation.room_id ?? "No Matrix room"}
                    </Text>
                    <Text size="xs" c="dimmed" mt={4}>
                      {conversation.message_count} messages · updated {timestamp(conversation.updated_at)}
                    </Text>
                  </Box>
                  <Text size="sm" c="dimmed" className="haku-conversation-open">
                    Open →
                  </Text>
                </Group>
              </button>
            ))
          )}
        </div>
      </div>
    </section>
  );
}

function ConversationDetailPage({ sessionId }: { sessionId: string }) {
  const [conversation, setConversation] = useState<ConversationSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const transcriptScrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  // Which conversation is on screen, for the two readers that must not close over a stale one:
  // the live-event callback, registered once, and a response landing after the operator moved on.
  const shownRef = useRef(sessionId);

  const read = useCallback(async () => {
    const requested = shownRef.current;
    try {
      const item = await fetchConversation(requested);
      if (shownRef.current === requested) {
        setConversation(item);
        setError(null);
      }
    } catch (reason: unknown) {
      if (shownRef.current === requested) setError(displayableError(reason));
    }
  }, []);
  const { refresh } = useCoalescedRefresh(read);

  useEffect(() => {
    // On mount the ref already holds this id and the live-event hook's own initial read covers
    // it; this effect exists for the operator opening a *different* transcript in place.
    if (shownRef.current === sessionId) return;
    shownRef.current = sessionId;
    setConversation(null);
    setError(null);
    stickToBottomRef.current = true;
    refresh();
  }, [sessionId, refresh]);

  // Live: the initial read, this session's own invalidations, and a catch-up on every reconnect.
  // A refetch rather than a delta stream is what makes a missed event cost nothing — the page
  // lands on the current transcript whether it heard one event or none.
  useConsoleEvents((event) => {
    if (event.event_type === "sync" || changedSessionId(event) === shownRef.current) refresh();
  });

  // A transcript opens on its newest message, the way the live chat surface does — the operator
  // came to read what just happened, not the first thing said. The layout settles a frame after
  // the transcript renders, so the scroll waits for it.
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

  const title = conversation.surface === "matrix" ? "Matrix conversation" : "Conversation";
  const narration = bootstrapNarration(conversation);
  const timeline = conversationTimeline(conversation.messages, conversation.turns);
  return (
    <section className="haku-page" aria-label="Conversation">
      <header className="haku-page-header">
        <div className="haku-page-bar haku-conversation-detail-header">
          <div>
            <Button variant="subtle" size="compact-sm" onClick={backToConversations}>
              ← Conversations
            </Button>
            <Title order={1}>{title}</Title>
            <Text c="dimmed" size="sm">
              {conversation.room_id ?? "Console chat session"} · started {timestamp(conversation.created_at)}
            </Text>
          </div>
          <Group gap="xs" wrap="nowrap" align="center">
            {/* The transcript is a lossy projection of the frame log, so an operator reading one
                that looks wrong needs the record it was projected from — one click away. */}
            <Button
              variant="light"
              size="compact-sm"
              onClick={() => navigateToConsolePath(sessionFramesPath(conversation.session_id))}
            >
              Raw frames
            </Button>
            <Badge color={statusColor(conversation.status)} variant="light">
              {conversation.status}
            </Badge>
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
            {narration && (
              <BootstrapNarrationPanel narration={narration} starting={conversation.status === "provisioning"} />
            )}
            {timeline.length === 0 && !narration && (
              <Text c="dimmed" size="sm">
                No transcript messages were recorded.
              </Text>
            )}
            {timeline.map((entry) =>
              entry.kind === "message" ? (
                <MessageView key={entry.message.message_id} message={entry.message} />
              ) : (
                <TurnBoundary key={entry.turn.turn_id} turn={entry.turn} number={entry.number} />
              )
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

export function ConversationsPage({ sessionId }: { sessionId: string | null }) {
  return sessionId === null ? <ConversationListPage /> : <ConversationDetailPage sessionId={sessionId} />;
}
