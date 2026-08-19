import { useEffect, useRef, useState } from "react";

import type { Conversation, ConversationFollowMessage } from "../client";
import { redirectToOperatorLogin } from "../operator_login";

// The one close the shell acts on instead of reconnecting: the operator session reached its
// absolute deadline, so every reconnect would be refused until the browser re-authenticates.
// Mirrors `OPERATOR_SESSION_EXPIRED_CLOSE_CODE` in ../../console_events.py.
const OPERATOR_SESSION_EXPIRED_CLOSE_CODE = 4001;

/** The health of a followed conversation, for chrome that shows when the view has stopped moving.
 *
 * `connecting` is the pre-open grace state, so nothing alarms during a handshake; `offline`
 * persists across reconnect attempts until one succeeds, so it does not blink between backoff
 * tries.
 */
export type FollowStatus = "connecting" | "live" | "offline";

function byId<T>(rows: readonly T[], id: (row: T) => string): Map<string, T> {
  return new Map(rows.map((row) => [id(row), row]));
}

/** Replace the rows of *held* that *arriving* carries, keeping the order the two agree on.
 *
 * Merged rather than appended because an update carries whole rows for anything that moved,
 * including rows already held — a message being written arrives once per coalescing window with
 * the prose so far. Order is by the field the server sorts on, so a row that arrives before the
 * one it follows still lands in the right place.
 */
function merged<T>(held: readonly T[], arriving: readonly T[], id: (row: T) => string, order: (row: T) => number): T[] {
  const replacing = byId(arriving, id);
  const kept = held.map((row) => replacing.get(id(row)) ?? row);
  const known = new Set(kept.map(id));
  return [...kept, ...arriving.filter((row) => !known.has(id(row)))].sort((left, right) => order(left) - order(right));
}

/** The conversation a follower holds after one message: a snapshot replaces, an update merges.
 *
 * The whole client half of the follow contract, in one pure function — there is no gap to detect
 * and no repair read to issue, because the server sends a snapshot itself whenever it cannot serve
 * a position. Merging is idempotent, so a message delivered twice or re-read from an older
 * position lands on the same conversation.
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
    session: {
      ...held.session,
      session_id: message.session_id,
      status: message.status,
      error: message.error,
      created_at: message.created_at,
      updated_at: message.updated_at,
      provisioning: message.provisioning,
      narration: message.narration,
      items: merged(
        held.session.items,
        message.items,
        (row) => row.item_id,
        (row) => Date.parse(row.created_at)
      ),
      turns: merged(
        held.session.turns,
        message.turns,
        (row) => row.turn_id,
        (row) => Date.parse(row.started_at)
      ),
    },
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
