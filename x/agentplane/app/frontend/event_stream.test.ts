// @vitest-environment happy-dom

import { create, type MessageInitShape } from "@bufbuild/protobuf";
import type { CallOptions, Client } from "@connectrpc/connect";
import { expect, it, vi } from "vitest";

import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import { AttachedSchema, HarnessState } from "../../runner/protocol_pb";
import {
  FollowEventsResponseSchema,
  type FollowEventsRequestSchema,
  type FollowEventsResponse,
  type ThreadEvents,
} from "../thread_events_pb";
import { appliedModel, catchingUp, EventStream } from "./event_stream";

const THREAD = "thread-1";

/** One server stream the test writes frames into, standing in for a connection. */
class Stream {
  private readonly queued: FollowEventsResponse[] = [];
  private waiting: (() => void) | null = null;
  private done = false;
  private broke = false;

  constructor(
    readonly afterCursor: bigint,
    private readonly signal: AbortSignal | undefined
  ) {}

  push(frame: MessageInitShape<typeof FollowEventsResponseSchema>): void {
    this.queued.push(create(FollowEventsResponseSchema, frame));
    this.waiting?.();
  }

  entry(entry: EventEntry): void {
    this.push({ frame: { case: "entry", value: entry } });
  }

  attached(lastCursor: bigint, model = "current-model"): void {
    this.push({
      frame: {
        case: "attached",
        value: create(AttachedSchema, { lastCursor, spec: { model }, harnessState: HarnessState.RUNNING }),
      },
    });
  }

  /** The transport dropping the connection, which is not the Thread ending. */
  break(): void {
    this.broke = true;
    this.done = true;
    this.waiting?.();
  }

  async *frames(): AsyncIterable<FollowEventsResponse> {
    for (;;) {
      // As a real transport does once its call is cancelled, rather than delivering into a reader
      // that has let go.
      if (this.signal?.aborted) throw new Error("aborted");
      while (this.queued.length) yield this.queued.shift() as FollowEventsResponse;
      if (this.done) {
        if (this.broke) throw new Error("connection reset");
        return;
      }
      await new Promise<void>((resolve) => {
        this.waiting = (): void => resolve();
        this.signal?.addEventListener("abort", this.waiting, { once: true });
      });
    }
  }
}

class Follows {
  readonly streams: Stream[] = [];

  get last(): Stream {
    return this.streams[this.streams.length - 1];
  }

  readonly client: Client<typeof ThreadEvents> = {
    followEvents: (
      request: MessageInitShape<typeof FollowEventsRequestSchema>,
      options?: CallOptions
    ): AsyncIterable<FollowEventsResponse> => {
      expect(request.threadId).toBe(THREAD);
      const stream = new Stream(BigInt(request.afterCursor ?? 0n), options?.signal);
      this.streams.push(stream);
      return stream.frames();
    },
  };
}

/** Runs every microtask the pushed frames produce. The store consumes one frame per microtask turn
 * and a macrotask lands after all of them, so this waits on the queue draining rather than on any
 * duration. */
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

function entry(cursor: bigint, fields: Omit<MessageInitShape<typeof EventEntrySchema>, "$typeName"> = {}): EventEntry {
  return create(EventEntrySchema, {
    cursor,
    origin: { sourceId: "runner", sequence: cursor },
    event: { observation: { case: "harnessStarted", value: {} } },
    ...fields,
  });
}

function open(): { store: EventStream; follows: Follows; unsubscribe: () => void } {
  const follows = new Follows();
  const store = new EventStream(follows.client, THREAD);
  return { store, follows, unsubscribe: store.subscribe(() => {}) };
}

it("shares one subscription, drops it on detach, and resumes only with its retained prefix", async () => {
  const follows = new Follows();
  const store = new EventStream(follows.client, THREAD);
  expect(follows.streams).toHaveLength(0);
  const first = store.subscribe(() => {});
  const second = store.subscribe(() => {});
  expect(follows.streams).toHaveLength(1);
  expect(follows.last.afterCursor).toBe(0n);

  const abandoned = follows.last;
  abandoned.entry(entry(1n));
  await settle();
  first();
  expect(follows.streams).toHaveLength(1);
  second();

  store.subscribe(() => {});
  expect(follows.streams).toHaveLength(2);
  expect(follows.last.afterCursor).toBe(1n);
  expect(store.getSnapshot().connection.kind).toBe("reconnecting");
  // An entry queued on a detached stream must not leak into its successor.
  abandoned.entry(entry(2n));
  await settle();
  expect(store.getSnapshot().conversation.lastCursor).toBe("1");
});

it("does not apply overlapping replayed entries twice or replace unchanged snapshots", async () => {
  const { store, follows } = open();
  const delta = entry(1n, {
    event: { observation: { case: "textDelta", value: { itemId: "reply", text: "hello" } } },
  });
  follows.last.entry(delta);
  await settle();
  const prefix = store.getSnapshot();
  follows.last.entry(delta);
  await settle();
  expect(store.getSnapshot()).toBe(prefix);
  expect(prefix.conversation.items.reply.text).toBe("hello");
  follows.last.entry(entry(2n));
  await settle();
  expect(prefix.conversation.entries).toHaveLength(1);
  expect(store.getSnapshot().conversation.entries).toHaveLength(2);
});

it.each([
  ["a gap", entry(3n)],
  ["a conflicting duplicate", entry(1n, { event: { observation: { case: "harnessLost", value: {} } } })],
  ["a different source", entry(2n, { origin: { sourceId: "replacement", sequence: 2n } })],
  ["a reassigned cursor", entry(2n, { origin: { sourceId: "runner", sequence: 1n } })],
  ["missing provenance", entry(2n, { origin: undefined })],
  ["a missing Event", entry(2n, { event: undefined })],
] as const)("stops at the verified prefix on %s", async (_description, invalid) => {
  const { store, follows, unsubscribe } = open();
  follows.last.entry(entry(1n));
  await settle();
  const prefix = store.getSnapshot().conversation;
  follows.last.entry(invalid);
  await settle();
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().conversation).toBe(prefix);
  follows.last.entry(entry(2n));
  await settle();
  expect(store.getSnapshot().conversation).toBe(prefix);
  expect(follows.streams).toHaveLength(1);
  unsubscribe();
  store.subscribe(() => {})();
  expect(follows.streams).toHaveLength(1);
});

it("treats a frame that carries nothing as a broken prefix", async () => {
  const { store, follows } = open();
  follows.last.push({});
  await settle();
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().conversation.lastCursor).toBe("0");
});

it("keeps snapshot@N separate from prefix@K and applies only later model changes over it", async () => {
  const { store, follows } = open();
  follows.last.attached(3n);
  await settle();
  expect(store.getSnapshot().conversation.harness).toBeNull();
  expect(store.getSnapshot().conversation.lastCursor).toBe("0");
  expect(catchingUp(store.getSnapshot())).toBe(true);
  expect(appliedModel(store.getSnapshot())).toBeNull();
  follows.last.entry(entry(1n));
  follows.last.entry(entry(2n, { event: { observation: { case: "modelChanged", value: { model: "old-model" } } } }));
  await settle();
  expect(appliedModel(store.getSnapshot())).toBeNull();
  follows.last.entry(entry(3n));
  await settle();
  expect(catchingUp(store.getSnapshot())).toBe(false);
  expect(appliedModel(store.getSnapshot())).toBe("current-model");
  follows.last.entry(entry(4n, { event: { observation: { case: "modelChanged", value: { model: "next-model" } } } }));
  await settle();
  expect(appliedModel(store.getSnapshot())).toBe("next-model");
});

it("reopens a dropped stream from its verified prefix and converges with a cold reader", async () => {
  const { store, follows } = open();
  follows.last.entry(entry(1n));
  await settle();
  follows.last.break();
  await settle();
  expect(store.getSnapshot().connection.kind).toBe("reconnecting");

  await vi.waitUntil(() => follows.streams.length === 2, { timeout: 5_000 });
  expect(follows.last.afterCursor).toBe(1n);
  follows.last.attached(2n);
  follows.last.entry(entry(2n));
  await settle();

  const cold = open();
  cold.follows.last.attached(2n);
  cold.follows.last.entry(entry(1n));
  cold.follows.last.entry(entry(2n));
  await settle();
  expect(store.getSnapshot()).toEqual(cold.store.getSnapshot());
});

it("does not reopen a completed stream", async () => {
  const { store, follows, unsubscribe } = open();
  follows.last.attached(1n);
  follows.last.entry(entry(1n));
  follows.last.push({ frame: { case: "ended", value: {} } });
  await settle();
  expect(store.getSnapshot().connection.kind).toBe("ended");
  unsubscribe();
  store.subscribe(() => {})();
  expect(follows.streams).toHaveLength(1);
});

it("surfaces ingestion that could not continue instead of reconnecting past it", async () => {
  const { store, follows } = open();
  follows.last.attached(1n);
  follows.last.entry(entry(1n));
  follows.last.push({ frame: { case: "ended", value: { error: "runner log cursor regressed" } } });
  await settle();
  expect(store.getSnapshot().connection).toEqual({ kind: "failed", reason: "runner log cursor regressed" });
  expect(follows.streams).toHaveLength(1);
});

it("rejects an end before the advertised prefix arrives", async () => {
  const { store, follows } = open();
  follows.last.attached(2n);
  follows.last.entry(entry(1n));
  follows.last.push({ frame: { case: "ended", value: {} } });
  await settle();
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().conversation.lastCursor).toBe("1");
});

it("rejects an attachment that rolls history backwards", async () => {
  const { store, follows } = open();
  follows.last.entry(entry(1n));
  follows.last.attached(0n);
  await settle();
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().conversation.lastCursor).toBe("1");
});

it("does not lower the advertised catch-up boundary on reconnect", async () => {
  const { store, follows } = open();
  follows.last.attached(3n);
  follows.last.entry(entry(1n));
  follows.last.attached(2n);
  await settle();
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().attached?.lastCursor).toBe(3n);
});
