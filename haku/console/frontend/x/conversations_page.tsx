import { Badge, Box, Button, Code, Group, Loader, Paper, Stack, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import {
  displayableError,
  fetchConversation,
  fetchConversations,
  type ClaudeChatMessage,
  type ConversationSession,
  type ConversationSessionSummary,
} from "../client";
import { CONVERSATIONS_PATH } from "../routing";
import { Markdown } from "./markdown";

function openConversation(sessionId: string): void {
  history.pushState(null, "", `${CONVERSATIONS_PATH}/${sessionId}`);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function backToConversations(): void {
  history.pushState(null, "", CONVERSATIONS_PATH);
  window.dispatchEvent(new PopStateEvent("popstate"));
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

function resultText(content: unknown): string {
  return typeof content === "string" ? content : JSON.stringify(content, null, 2);
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
      {message.tool_uses.length > 0 && (
        <Stack gap="xs" mb="sm">
          {message.tool_uses.map((toolUse) => (
            <Paper key={toolUse.tool_use_id} withBorder p="sm" radius="sm">
              <Group gap="xs" mb="xs">
                <Badge variant="light" color="gray">
                  Tool
                </Badge>
                <Code style={{ overflowWrap: "anywhere" }}>{toolUse.name}</Code>
                {toolUse.result?.is_error && (
                  <Badge variant="light" color="red">
                    failed
                  </Badge>
                )}
              </Group>
              <Code block style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {JSON.stringify(toolUse.input, null, 2)}
              </Code>
              {toolUse.result && (
                <>
                  <Text c="dimmed" size="xs" mt="xs" mb={4}>
                    Result
                  </Text>
                  <Code
                    block
                    c={toolUse.result.is_error ? "red" : undefined}
                    style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
                  >
                    {resultText(toolUse.result.content)}
                  </Code>
                </>
              )}
            </Paper>
          ))}
        </Stack>
      )}
      <Markdown
        source={message.content || (message.status === "streaming" ? "…" : "")}
        className="haku-chat-markdown"
      />
      {message.error && (
        <Text c="red" size="xs" mt="xs">
          {message.error}
        </Text>
      )}
    </Paper>
  );
}

function ConversationListPage() {
  const [conversations, setConversations] = useState<ConversationSessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchConversations()
      .then((items) => {
        if (alive) setConversations(items);
      })
      .catch((reason: unknown) => {
        if (alive) setError(displayableError(reason));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

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
            {conversations.length} sessions
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
          {conversations.length === 0 && loading ? (
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

  useEffect(() => {
    let alive = true;
    setConversation(null);
    setError(null);
    fetchConversation(sessionId)
      .then((item) => {
        if (alive) setConversation(item);
      })
      .catch((reason: unknown) => {
        if (alive) setError(displayableError(reason));
      });
    return () => {
      alive = false;
    };
  }, [sessionId]);

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
          <Badge color={statusColor(conversation.status)} variant="light">
            {conversation.status}
          </Badge>
        </div>
      </header>
      <div className="haku-conversation-detail-body">
        <div className="haku-conversation-transcript-scroll">
          <div className="haku-page-list haku-chat-messages">
            {conversation.messages.length === 0 ? (
              <Text c="dimmed" size="sm">
                No transcript messages were recorded.
              </Text>
            ) : (
              conversation.messages.map((message) => <MessageView key={message.message_id} message={message} />)
            )}
          </div>
        </div>
        <aside className="haku-conversation-turns" aria-label="Conversation turns">
          <Text fw={600} size="sm">
            Turns
          </Text>
          <Text c="dimmed" size="xs">
            Exchange summaries; raw agent frames can be linked later.
          </Text>
          {conversation.turns.length === 0 ? (
            <Text c="dimmed" size="sm">
              No completed turns recorded.
            </Text>
          ) : (
            conversation.turns.map((turn, index) => (
              <Paper key={turn.turn_id} withBorder p="sm">
                <Group justify="space-between" mb={4}>
                  <Text fw={600} size="sm">
                    Turn {conversation.turns.length - index}
                  </Text>
                  <Badge size="xs" color={turn.outcome === "failed" ? "red" : "teal"} variant="light">
                    {turn.outcome ?? "running"}
                  </Badge>
                </Group>
                <Text size="xs" c="dimmed">
                  started {timestamp(turn.started_at)}
                </Text>
                {turn.duration_ms != null && (
                  <Text size="xs" c="dimmed">
                    duration {(turn.duration_ms / 1000).toFixed(1)}s
                  </Text>
                )}
                {turn.cost_usd != null && (
                  <Text size="xs" c="dimmed">
                    cost ${turn.cost_usd.toFixed(4)}
                  </Text>
                )}
              </Paper>
            ))
          )}
        </aside>
      </div>
    </section>
  );
}

export function ConversationsPage({ sessionId }: { sessionId: string | null }) {
  return sessionId === null ? <ConversationListPage /> : <ConversationDetailPage sessionId={sessionId} />;
}
