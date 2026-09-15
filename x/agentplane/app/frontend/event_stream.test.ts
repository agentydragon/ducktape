// @vitest-environment happy-dom

import { create, toJsonString, type MessageInitShape } from "@bufbuild/protobuf";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import { AttachedSchema, HarnessState } from "../../runner/protocol_pb";
import { appliedModel, catchingUp, EventStream } from "./event_stream";

class Source extends EventTarget {
  static instances: Source[] = [];
  closed = false;

  constructor(readonly url: string) {
    super();
    Source.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  entry(entry: EventEntry, lastEventId = String(entry.cursor)): void {
    this.dispatchEvent(new MessageEvent("event", { data: toJsonString(EventEntrySchema, entry), lastEventId }));
  }

  attached(cursor: bigint, model = "current-model"): void {
    this.dispatchEvent(
      new MessageEvent("attached", {
        data: toJsonString(
          AttachedSchema,
          create(AttachedSchema, { lastCursor: cursor, spec: { model }, harnessState: HarnessState.RUNNING })
        ),
      })
    );
  }
}

beforeEach(() => {
  Source.instances = [];
  vi.stubGlobal("EventSource", Source);
});

afterEach(() => vi.unstubAllGlobals());

function entry(cursor: bigint, fields: MessageInitShape<typeof EventEntrySchema> = {}): EventEntry {
  return create(EventEntrySchema, {
    cursor,
    origin: { sourceId: "runner", sequence: cursor },
    event: { observation: { case: "harnessStarted", value: {} } },
    ...fields,
  });
}

function open(): { store: EventStream; source: Source; unsubscribe: () => void } {
  const store = new EventStream("/events");
  const unsubscribe = store.subscribe(() => {});
  return { store, source: Source.instances[Source.instances.length - 1], unsubscribe };
}

it("shares one subscription, closes on detach, and resumes only with its retained prefix", () => {
  const store = new EventStream("/events?limit=100");
  expect(Source.instances).toHaveLength(0);
  const first = store.subscribe(() => {});
  const second = store.subscribe(() => {});
  expect(Source.instances).toHaveLength(1);
  const source = Source.instances[0];
  expect(new URL(source.url).searchParams.get("after")).toBe("0");
  source.entry(entry(1n));
  first();
  expect(source.closed).toBe(false);
  second();
  expect(source.closed).toBe(true);
  const unsubscribe = store.subscribe(() => {});
  expect(new URL(Source.instances[1].url).searchParams.get("after")).toBe("1");
  expect(store.getSnapshot().connection.kind).toBe("reconnecting");
  expect(new URL(Source.instances[1].url).searchParams.get("limit")).toBe("100");
  source.entry(entry(2n)); // An event queued by a detached transport must not leak into its successor.
  expect(store.getSnapshot().conversation.lastCursor).toBe("1");
  unsubscribe();
  const fresh = new EventStream("/events");
  fresh.subscribe(() => {})();
  expect(new URL(Source.instances[2].url).searchParams.get("after")).toBe("0");
});

it("does not apply overlapping streaming deltas twice or replace unchanged snapshots", () => {
  const { store, source, unsubscribe } = open();
  const delta = entry(1n, {
    event: { observation: { case: "textDelta", value: { itemId: "reply", text: "hello" } } },
  });
  source.entry(delta);
  const prefix = store.getSnapshot();
  source.entry(delta);
  expect(store.getSnapshot()).toBe(prefix);
  expect(prefix.conversation.items.reply.text).toBe("hello");
  source.entry(entry(2n));
  expect(prefix.conversation.entries).toHaveLength(1);
  expect(store.getSnapshot().conversation.entries).toHaveLength(2);
  unsubscribe();
});

it.each([
  ["a gap", entry(3n)],
  ["a conflicting duplicate", entry(1n, { event: { observation: { case: "harnessLost", value: {} } } })],
  ["a different source", entry(2n, { origin: { sourceId: "replacement", sequence: 2n } })],
  ["a reassigned cursor", entry(2n, { origin: { sourceId: "runner", sequence: 1n } })],
  ["missing provenance", entry(2n, { origin: undefined })],
  ["a missing Event", entry(2n, { event: undefined })],
] as const)("stops at the verified prefix on %s", (_description, invalid) => {
  const { store, source, unsubscribe } = open();
  source.entry(entry(1n));
  const prefix = store.getSnapshot().conversation;
  source.entry(invalid);
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().conversation).toBe(prefix);
  expect(source.closed).toBe(true);
  source.entry(entry(2n));
  expect(store.getSnapshot().conversation).toBe(prefix);
  unsubscribe();
  store.subscribe(() => {})();
  expect(Source.instances).toHaveLength(1);
});

it.each(["{bad json", '{"cursor":"1","unknownField":true}'])("makes a decoding failure visible: %s", (data) => {
  const { store, source } = open();
  source.dispatchEvent(new MessageEvent("event", { data }));
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().conversation.lastCursor).toBe("0");
  expect(source.closed).toBe(true);
});

it("rejects an SSE resume id that does not identify its Event", () => {
  const { store, source } = open();
  source.entry(entry(1n), "9");
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().conversation.lastCursor).toBe("0");
});

it("keeps snapshot@N separate from prefix@K and applies only later model changes over it", () => {
  const { store, source } = open();
  source.attached(3n);
  expect(store.getSnapshot().conversation.harness).toBeNull();
  expect(store.getSnapshot().conversation.lastCursor).toBe("0");
  expect(catchingUp(store.getSnapshot())).toBe(true);
  expect(appliedModel(store.getSnapshot())).toBeNull();
  source.entry(entry(1n));
  source.entry(entry(2n, { event: { observation: { case: "modelChanged", value: { model: "old-model" } } } }));
  expect(appliedModel(store.getSnapshot())).toBeNull();
  source.entry(entry(3n));
  expect(catchingUp(store.getSnapshot())).toBe(false);
  expect(appliedModel(store.getSnapshot())).toBe("current-model");
  source.entry(entry(4n, { event: { observation: { case: "modelChanged", value: { model: "next-model" } } } }));
  expect(appliedModel(store.getSnapshot())).toBe("next-model");
});

it("keeps native reconnect active and converges after overlapping catch-up", () => {
  const { store, source } = open();
  source.entry(entry(1n));
  source.dispatchEvent(new Event("error"));
  expect(store.getSnapshot().connection.kind).toBe("reconnecting");
  expect(source.closed).toBe(false);
  source.attached(2n);
  source.entry(entry(1n));
  source.entry(entry(2n));
  const cold = open();
  cold.source.attached(2n);
  cold.source.entry(entry(1n));
  cold.source.entry(entry(2n));
  expect(store.getSnapshot()).toEqual(cold.store.getSnapshot());
});

it("does not reopen a completed stream", () => {
  const { store, source, unsubscribe } = open();
  source.attached(1n);
  source.entry(entry(1n));
  source.dispatchEvent(new Event("end"));
  expect(store.getSnapshot().connection.kind).toBe("ended");
  expect(source.closed).toBe(true);
  unsubscribe();
  store.subscribe(() => {})();
  expect(Source.instances).toHaveLength(1);
});

it("rejects an end before the advertised prefix arrives", () => {
  const { store, source } = open();
  source.attached(2n);
  source.entry(entry(1n));
  source.dispatchEvent(new Event("end"));
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().conversation.lastCursor).toBe("1");
});

it("rejects an attachment that rolls history backwards", () => {
  const { store, source } = open();
  source.entry(entry(1n));
  source.attached(0n);
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().conversation.lastCursor).toBe("1");
});

it("does not lower the advertised catch-up boundary on reconnect", () => {
  const { store, source } = open();
  source.attached(3n);
  source.entry(entry(1n));
  source.attached(2n);
  expect(store.getSnapshot().connection.kind).toBe("failed");
  expect(store.getSnapshot().attached?.lastCursor).toBe(3n);
});
