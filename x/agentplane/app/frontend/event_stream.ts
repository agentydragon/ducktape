import { equals, fromJson, type JsonValue } from "@bufbuild/protobuf";

import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import { AttachedSchema, type Attached } from "../../runner/protocol_pb";
import { EMPTY, reduce, type SessionState } from "./events";

export type Connection =
  { kind: "connecting" | "following" | "reconnecting" | "ended" } | { kind: "failed"; reason: string };

export interface StreamSnapshot {
  conversation: SessionState;
  /** Operational snapshot at lastCursor, not an entry or a consumed replay cursor. */
  attached: Attached | null;
  connection: Connection;
}

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

/** One subscribed target owns one verified prefix. A fresh instance replays from zero; a cursor
 * without its corresponding Events is never a valid starting state. Construction has no effects. */
export class EventStream {
  private snapshot: StreamSnapshot = {
    conversation: EMPTY,
    attached: null,
    connection: { kind: "connecting" },
  };
  private readonly listeners = new Set<() => void>();
  private source: EventSource | null = null;

  constructor(private readonly url: string) {}

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
    this.source?.close();
    this.source = null;
  }

  private fail(reason: unknown): void {
    this.disconnect();
    this.publish({
      ...this.snapshot,
      connection: { kind: "failed", reason: reason instanceof Error ? reason.message : String(reason) },
    });
  }

  private accept(entry: EventEntry): void {
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
    const url = new URL(this.url, window.location.href);
    url.searchParams.set("after", this.snapshot.conversation.lastCursor);
    const source = new EventSource(url.href);
    this.source = source;
    source.addEventListener("open", () => {
      if (this.source === source) this.publish({ ...this.snapshot, connection: { kind: "following" } });
    });
    source.addEventListener("attached", (message: MessageEvent<string>) => {
      if (this.source !== source) return;
      try {
        const attached = fromJson(AttachedSchema, JSON.parse(message.data) as JsonValue);
        if (attached.lastCursor < BigInt(this.snapshot.conversation.lastCursor)) {
          throw new Error("Attachment snapshot is behind the consumed Event prefix");
        }
        if (this.snapshot.attached && attached.lastCursor < this.snapshot.attached.lastCursor) {
          throw new Error("Attachment snapshot moved its advertised Event prefix backwards");
        }
        this.publish({ ...this.snapshot, attached, connection: { kind: "following" } });
      } catch (error) {
        this.fail(error);
      }
    });
    source.addEventListener("event", (message: MessageEvent<string>) => {
      if (this.source !== source) return;
      try {
        const entry = fromJson(EventEntrySchema, JSON.parse(message.data) as JsonValue);
        if (message.lastEventId && message.lastEventId !== String(entry.cursor)) {
          throw new Error(`SSE id does not match Event cursor ${entry.cursor}`);
        }
        this.accept(entry);
      } catch (error) {
        // EventSource has already consumed the wire id. Do not let it reconnect past a rejected
        // entry: preserve the verified prefix and make the broken stream visible instead.
        this.fail(error);
      }
    });
    source.addEventListener("end", () => {
      if (this.source !== source) return;
      if (catchingUp(this.snapshot)) {
        this.fail("Event stream ended before its advertised prefix was replayed");
        return;
      }
      this.disconnect();
      this.publish({ ...this.snapshot, connection: { kind: "ended" } });
    });
    source.addEventListener("error", (message: globalThis.Event) => {
      if (this.source !== source) return;
      if ("data" in message) {
        this.fail((message as MessageEvent<string>).data);
      } else {
        // Native EventSource reconnects with Last-Event-ID. Only successfully verified entries
        // can reach this path; integrity failures close it above.
        this.publish({ ...this.snapshot, connection: { kind: "reconnecting" } });
      }
    });
  }
}
