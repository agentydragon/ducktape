import { useEffect, useRef, useState } from "react";

import type { Conversation, ConversationEntry, ConversationFollowMessage } from "../client";
import { redirectToOperatorLogin } from "../operator_login";

// The one close the shell acts on instead of reconnecting: the operator session reached its
// absolute deadline, so every reconnect would be refused until the browser re-authenticates.
// Mirrors `OPERATOR_SESSION_EXPIRED_CLOSE_CODE` in ../../notifications/console_events.py.
const OPERATOR_SESSION_EXPIRED_CLOSE_CODE = 4001;

/** The health of a followed conversation, for chrome that shows when the view has stopped moving.
 *
 * `connecting` is the pre-open grace state, so nothing alarms during a handshake; `offline`
 * persists across reconnect attempts until one succeeds, so it does not blink between backoff
 * tries.
 */
export type FollowStatus = "connecting" | "live" | "offline";

/** Merge the rows an update carries over the ones held, replacing by position.
 *
 * Replace rather than append: a row arrives whole in its current state, and arrives again when it
 * changes — a message being written once per coalescing window with the prose so far — so the
 * newest copy wins, a duplicate costs nothing, and an entry that arrives out of order still lands
 * at its position.
 */
function mergedEntries(
  held: readonly ConversationEntry[],
  arriving: readonly ConversationEntry[]
): ConversationEntry[] {
  const byPosition = new Map<number, ConversationEntry>(held.map((entry) => [entry.opened_seq, entry]));
  for (const entry of arriving) byPosition.set(entry.opened_seq, entry);
  return [...byPosition.values()].sort((left, right) => left.opened_seq - right.opened_seq);
}

/** The conversation a follower holds after one message: a snapshot replaces, an update merges.
 *
 * The whole client half of the follow contract, in one pure function — there is no gap to detect
 * and no repair read to issue, because the server sends a snapshot itself whenever it cannot serve
 * a position. Only the entries merge — by replacement, since a row re-arrives whole when it
 * changes; everything else the update carries arrives whole and replaces what is held, the
 * session block included, so nothing a follower shows can belong to a session it has just been
 * told was replaced.
 *
 * An update with nothing to merge into is a protocol violation rather than a state to render: a
 * position is only ever sent back after a snapshot established what it addresses.
 */
export function followed(held: Conversation | null, message: ConversationFollowMessage): Conversation {
  if (message.message_type === "snapshot") return message.conversation;
  if (held === null) throw new Error("a conversation update arrived before any snapshot");
  return {
    ...held,
    attachments: message.attachments,
    earlier_sessions: message.earlier_sessions,
    entries: mergedEntries(held.entries, message.entries),
    session: message.session,
    provisioning: message.provisioning,
    narration: message.narration,
  };
}

function followUrl(conversationId: string, position: number | null): URL | null {
  // Resolved against the document base, which is what the REST client's relative "/api/…" URLs
  // resolve against too — so a page served from a sub-path, and a harness that renders with a
  // `<base href>`, both address the same API this does.
  const url = new URL(`/api/conversations/${encodeURIComponent(conversationId)}/follow`, document.baseURI);
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (position !== null) url.searchParams.set("after", String(position));
  return url;
}

/** Follow one conversation for as long as it is on screen.
 *
 * One socket per conversation, opened on mount and re-opened with the position of the last message
 * it received — the same call, so a reconnect costs the changes since that position rather than
 * the transcript. Nothing polls: what a tab shows moves when the conversation does.
 */
export function useFollowedConversation(conversationId: string): {
  conversation: Conversation | null;
  status: FollowStatus;
  error: string | null;
} {
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [status, setStatus] = useState<FollowStatus>("connecting");
  const [error, setError] = useState<string | null>(null);

  // What the next connection asks from, and what it holds. Refs rather than state because a
  // reconnect reads them from inside a closure the effect installed once.
  const position = useRef<number | null>(null);
  const held = useRef<Conversation | null>(null);

  useEffect(() => {
    position.current = null;
    held.current = null;
    setConversation(null);
    setError(null);
    setStatus("connecting");

    let closed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let backoffMs = 1000;

    const connect = () => {
      if (closed) return;
      const url = followUrl(conversationId, position.current);
      if (url === null) return;
      socket = new WebSocket(url);
      socket.onopen = () => {
        if (closed) return;
        backoffMs = 1000;
        setStatus("live");
      };
      socket.onmessage = (event) => {
        if (closed) return;
        try {
          const message = JSON.parse(String(event.data)) as ConversationFollowMessage;
          const next = followed(held.current, message);
          held.current = next;
          position.current = message.position;
          setConversation(next);
          setError(null);
        } catch (reason) {
          // A message this bundle cannot apply leaves the view where it was rather than half
          // updated; the socket keeps running, and the next snapshot replaces the lot.
          console.error("Invalid conversation follow message", reason);
        }
      };
      // onerror always precedes onclose; let onclose drive the state and the reconnect so a failed
      // handshake and a dropped connection funnel through one path.
      socket.onclose = (event) => {
        if (closed) return;
        // An expired session is not a channel outage: reconnecting would just be refused, and a
        // spinner would blame the network for an auth problem.
        if (event.code === OPERATOR_SESSION_EXPIRED_CLOSE_CODE) {
          redirectToOperatorLogin();
          return;
        }
        if (event.code === 1008 && held.current === null) setError(event.reason || "Conversation not found");
        setStatus("offline");
        reconnectTimer = window.setTimeout(connect, backoffMs);
        backoffMs = Math.min(backoffMs * 2, 30_000);
      };
    };
    connect();

    return () => {
      closed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [conversationId]);

  return { conversation, status, error };
}
