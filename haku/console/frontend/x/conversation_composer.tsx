import { Alert, Button, Group, Stack, Text, Textarea } from "@mantine/core";
import { useState } from "react";

import { abortSessionTurn, displayableError, PromptRefused, sendChatPrompt, type ConversationSession } from "../client";

function placeholder(status: ConversationSession["status"]): string {
  if (status === "ready") return "Send a message…";
  if (status === "responding") return "A turn is running — a message sent now would be refused.";
  return "The session is not ready for a message yet.";
}

/** Send into an existing session, whichever surface opened it.
 *
 * Nothing here asks what the session's `surface` is, because the route does not either: a Matrix
 * room's session takes a prompt from the browser on the same terms as one the SPA created. The
 * reply goes wherever that session's channel sends replies, so a prompt typed here also lands in
 * the room.
 *
 * **A refusal is the case worth getting right.** `enqueue_prompt` rejects a mid-turn prompt and
 * rejects a second prompt while one is still queued — and the queued case leaves the session
 * `ready`, so the disabled Send below cannot pre-empt it. The operator's text exists nowhere but
 * this box until the console accepts it, so a refusal keeps it and says why.
 */
export function ConversationComposer({
  sessionId,
  status,
  onSent,
}: {
  sessionId: string;
  status: ConversationSession["status"];
  onSent: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [aborting, setAborting] = useState(false);
  // Held apart from `error` because they mean opposite things: a refusal is the console answering,
  // so the prompt certainly was not recorded, while an error leaves its fate unknown.
  const [refusal, setRefusal] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function send() {
    const prompt = text.trim();
    if (!prompt || busy) return;
    setBusy(true);
    setRefusal(null);
    setError(null);
    try {
      await sendChatPrompt(sessionId, prompt);
      setText("");
      onSent();
    } catch (reason: unknown) {
      if (reason instanceof PromptRefused) setRefusal(reason.message);
      else setError(displayableError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function abort() {
    setAborting(true);
    setError(null);
    try {
      await abortSessionTurn(sessionId);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    } finally {
      setAborting(false);
    }
  }

  return (
    <div className="haku-chat-composer">
      <Stack gap="xs" className="haku-chat-composer-inner">
        {refusal && (
          <Alert color="orange" variant="light" title="Not sent" role="status">
            <Text size="sm">{refusal}</Text>
            <Text size="xs" c="dimmed" mt={4}>
              Your message is still below — send it again once the session will take it.
            </Text>
          </Alert>
        )}
        {error && (
          <Alert color="red" variant="light" title="The send failed" role="status">
            <Text size="sm">{error}</Text>
          </Alert>
        )}
        <Textarea
          aria-label="Message"
          placeholder={placeholder(status)}
          autosize
          minRows={2}
          maxRows={8}
          value={text}
          disabled={busy}
          onChange={(event) => setText(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void send();
          }}
        />
        <Group justify="flex-end">
          <Text c="dimmed" size="xs">
            Ctrl/⌘ + Enter to send
          </Text>
          {status === "responding" && (
            <Button variant="light" color="orange" onClick={() => void abort()} loading={aborting}>
              Abort
            </Button>
          )}
          <Button onClick={() => void send()} disabled={status !== "ready" || text.trim().length === 0} loading={busy}>
            Send
          </Button>
        </Group>
      </Stack>
    </div>
  );
}
