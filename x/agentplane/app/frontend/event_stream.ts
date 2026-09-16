import { equals } from "@bufbuild/protobuf";
import type { Client } from "@connectrpc/connect";

import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import type { Attached } from "../../runner/protocol_pb";
import type { FollowEventsResponse, ThreadEvents } from "../thread_events_pb";
import { EMPTY, reduce, type SessionState } from "./events";

export type Connection =
  { kind: "connecting" | "following" | "reconnecting" | "ended" } | { kind: "failed"; reason: string };

export interface StreamSnapshot {
  conversation: SessionState;
  /** Operational snapshot at lastCursor, not an entry or a consumed replay cursor. */
  attached: Attached | null;
  connection: Connection;
}

/** How long before reopening a stream the transport dropped, doubling to `RECONNECT_MAX_MS` while
 * it keeps dropping. The cursor a reader holds is verified, so reopening from it is always safe --
 * the delay is only there to keep a server that is down from being hammered. */
const RECONNECT_MS = 500;
const RECONNECT_MAX_MS = 8_000;

export function catchingUp(snapshot: StreamSnapshot): boolean {
  return snapshot.attached !== null && BigInt(snapshot.conversation.lastCursor) < snapshot.attached.lastCursor;
}

/** Do not mix snapshot@N with model Events from an earlier, partially replayed prefix. */
export function appliedModel(snapshot: StreamSnapshot): string | null {
  if (catchingUp(snapshot)) return null;
  const boundary = snapshot.attached?.lastCursor ?? 0n;
  for (let index = snapshot.conversation.entries.length - 1; index >= 0; index--) {
    const entry = snapshot.conversation.entries[index];
    if (entry.cursor <= boundary) break;
    if (entry.event?.observation.case === "modelChanged") return entry.event.observation.value.model;
  }
  return snapshot.attached?.spec?.model || null;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    let timer = 0;
    const done = (): void => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    timer = window.setTimeout(done, ms);
    signal.addEventListener("abort", done);
  });
}

/** One subscribed target owns one verified prefix. A fresh instance replays from zero; a cursor
 * without its corresponding Events is never a valid starting state. Construction has no effects. */
export class EventStream {
  private snapshot: StreamSnapshot = {
    conversation: EMPTY,
    attached: null,
    connection: { kind: "connecting" },
  };
  private readonly listeners = new Set<() => void>();
  private following: AbortController | null = null;

  constructor(
    private readonly client: Client<typeof ThreadEvents>,
    private readonly threadId: string
  ) {}

  getSnapshot = (): StreamSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) this.connect();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.disconnect();
    };
  };

  private publish(snapshot: StreamSnapshot): void {
    this.snapshot = snapshot;
    for (const listener of this.listeners) listener();
  }

  private disconnect(): void {
    this.following?.abort();
    this.following = null;
  }

  private fail(reason: unknown): void {
    this.disconnect();
    this.publish({
      ...this.snapshot,
      connection: { kind: "failed", reason: reason instanceof Error ? reason.message : String(reason) },
    });
  }

  /** False once the stream must not be reopened: it completed, or something in it broke the
   * prefix. A transport failure is neither, and does not reach here. */
  private accept(response: FollowEventsResponse): boolean {
    try {
      switch (response.frame.case) {
        case "heartbeat":
          this.publish({ ...this.snapshot, connection: { kind: "following" } });
          return true;
        case "attached":
          this.attach(response.frame.value);
          return true;
        case "entry":
          this.entry(response.frame.value);
          return true;
        case "ended":
          if (catchingUp(this.snapshot)) {
            throw new Error("Event stream ended before its advertised prefix was replayed");
          }
          // Ingestion that could not continue is the archive's failure, not the transport's: there
          // is nothing further to reconnect to, and the reason is what the reader needs.
          if (response.frame.value.error !== undefined) throw new Error(response.frame.value.error);
          this.disconnect();
          this.publish({ ...this.snapshot, connection: { kind: "ended" } });
          return false;
        case undefined:
          throw new Error("Stream frame carries nothing");
      }
    } catch (error) {
      this.fail(error);
      return false;
    }
  }

  private attach(attached: Attached): void {
    if (attached.lastCursor < BigInt(this.snapshot.conversation.lastCursor)) {
      throw new Error("Attachment snapshot is behind the consumed Event prefix");
    }
    if (this.snapshot.attached && attached.lastCursor < this.snapshot.attached.lastCursor) {
      throw new Error("Attachment snapshot moved its advertised Event prefix backwards");
    }
    this.publish({ ...this.snapshot, attached, connection: { kind: "following" } });
  }

  private entry(entry: EventEntry): void {
    const previous = this.snapshot.conversation;
    if (entry.cursor < 1n || !entry.origin?.sourceId || entry.origin.sequence !== entry.cursor || !entry.event) {
      throw new Error(`Invalid Event provenance or payload at cursor ${entry.cursor}`);
    }
    if (entry.cursor <= BigInt(previous.lastCursor)) {
      // The in-memory prefix starts at one and is dense; this index is bounded by its array length.
      if (!equals(EventEntrySchema, previous.entries[Number(entry.cursor) - 1], entry)) {
        throw new Error(`Conflicting Event at cursor ${entry.cursor}`);
      }
      return;
    }
    const expected = BigInt(previous.lastCursor) + 1n;
    if (entry.cursor !== expected) throw new Error(`Event gap: expected ${expected}, received ${entry.cursor}`);
    if (previous.entries.length && entry.origin.sourceId !== previous.entries[0].origin?.sourceId) {
      throw new Error(`Event source changed at cursor ${entry.cursor}`);
    }
    this.publish({ ...this.snapshot, conversation: reduce(previous, entry), connection: { kind: "following" } });
  }

  private connect(): void {
    if (this.snapshot.connection.kind === "ended" || this.snapshot.connection.kind === "failed") return;
    const controller = new AbortController();
    this.following = controller;
    void this.follow(controller);
  }

  /** Reopens for as long as this target is subscribed, always from the prefix it has verified.
   * Only `accept` ends the loop for good; anything the transport does is a reconnect, which is
   * what the browser's own EventSource did before this and what a page left open overnight needs.
   */
  private async follow(controller: AbortController): Promise<void> {
    let backoff = RECONNECT_MS;
    this.publish({
      ...this.snapshot,
      connection: { kind: this.snapshot.conversation.lastCursor === "0" ? "connecting" : "reconnecting" },
    });
    while (!controller.signal.aborted) {
      try {
        const responses = this.client.followEvents(
          { threadId: this.threadId, afterCursor: BigInt(this.snapshot.conversation.lastCursor) },
          { signal: controller.signal }
        );
        for await (const response of responses) {
          // A transport that has not noticed the abort yet must not deliver into a reader that has
          // let go: this target's prefix belongs to whoever is subscribed now.
          if (controller.signal.aborted) return;
          backoff = RECONNECT_MS;
          if (!this.accept(response)) return;
        }
      } catch (error) {
        // The stream broke rather than ending. What was accepted from it stays accepted, and the
        // reconnect below is silent to the reader, so this is the only place it is visible at all.
        if (!controller.signal.aborted) console.warn(`thread ${this.threadId}: event stream broke`, error);
      }
      if (controller.signal.aborted) return;
      // Said the moment the connection goes, not when the next attempt starts: the reader is
      // already not being told anything, and the backoff below is most of the gap.
      this.publish({ ...this.snapshot, connection: { kind: "reconnecting" } });
      await sleep(backoff, controller.signal);
      backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
    }
  }
}
