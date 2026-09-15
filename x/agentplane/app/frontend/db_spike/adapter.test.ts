import { create, equals, fromJson, toJson } from "@bufbuild/protobuf";
import { createLiveQueryCollection, eq } from "@tanstack/db";
import { afterEach, expect, it, vi } from "vitest";

import { CommandSchema } from "../../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../../protocol/event_log_pb";
import { View, type Entity, type Row, type Snapshot } from "./adapter";

const SOURCE = "runner-source";
const H = 9_007_199_254_740_993n;
const views: View[] = [];

afterEach(async () => {
  await Promise.all(views.splice(0).map((view) => view.close()));
});

function view(): View {
  const result = new View();
  views.push(result);
  return result;
}

function item(key: string, revision: bigint, text: string): Entity {
  return { key, revision, value: { kind: "item", text } };
}

function receipt(cursor: bigint): EventEntry {
  return create(EventEntrySchema, {
    cursor,
    origin: { sourceId: SOURCE, sequence: cursor },
    event: {
      observation: {
        case: "commandAdmitted",
        value: {
          command: create(CommandSchema, {
            commandId: "model",
            operation: { case: "changeModel", value: { model: "next" } },
          }),
        },
      },
    },
  });
}

function snapshot(rows: readonly Entity[], through = H): Promise<Snapshot> {
  return Promise.resolve({ source: SOURCE, through, rows });
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

it("publishes rows, command effects, controls, and coverage together without React batching", async () => {
  const store = view();
  const seen: Row[][] = [];
  const subscription = store.rows.subscribeChanges(() => {
    seen.push(Array.from(store.rows.values()));
  });
  const admission = receipt(H - 10n);
  const initial: Entity[] = [
    item("item:a", H, "partial"),
    { key: "control", revision: H, value: { kind: "control", model: "old" } },
    { key: "command:model", revision: H - 10n, value: { kind: "command", admission, outcome: null } },
  ];
  const follow = await store.bootstrap(snapshot(initial));
  if (!follow) throw new Error("Bootstrap was superseded");
  const before = Array.from(store.rows.values());
  const outcome = create(EventEntrySchema, {
    cursor: H + 1n,
    origin: { sourceId: SOURCE, sequence: H + 1n },
    event: {
      observation: { case: "modelChanged", value: { commandId: "model", previousModel: "old", model: "next" } },
    },
  });
  await follow({
    source: SOURCE,
    after: H,
    through: H + 1n,
    evict: [],
    rows: [
      item("item:a", H + 1n, "complete"),
      { key: "control", revision: H + 1n, value: { kind: "control", model: "next" } },
      { key: "command:model", revision: H + 1n, value: { kind: "command", admission, outcome } },
    ],
  });
  const after = Array.from(store.rows.values());
  expect(seen.length).toBeGreaterThanOrEqual(2);
  for (const state of seen) expect([before, after]).toContainEqual(state);
  expect(before).not.toEqual(after);
  expect(store.coverage.revision).toBe(H + 1n);
  subscription.unsubscribe();
});

it("preserves generated protobuf uint64 and exact receipts through collection storage and queries", async () => {
  const store = view();
  const admission = fromJson(EventEntrySchema, toJson(EventEntrySchema, receipt(H)));
  await store.bootstrap(
    snapshot([
      { key: "command:model", revision: H, value: { kind: "command", admission, outcome: null } },
      item("item:earlier", H - 1n, "earlier"),
    ])
  );
  const ordered = createLiveQueryCollection((q) => q.from({ row: store.rows }).orderBy(({ row }) => row.revision));
  try {
    await ordered.preload();
    expect(ordered.toArray.map((row) => row.revision)).toEqual([H - 1n, H, H]);
    const command = ordered.toArray.find((row) => row.value.kind === "command");
    if (command?.value.kind !== "command") throw new Error("Missing command");
    expect(command.value.admission.cursor).toBe(H);
    expect(equals(EventEntrySchema, command.value.admission, receipt(H))).toBe(true);
  } finally {
    await ordered.cleanup();
  }
});

it("keeps item subscriptions quiet for unrelated rows and coverage-only progress", async () => {
  const store = view();
  const follow = await store.bootstrap(snapshot([item("item:a", H, "a"), item("item:b", H, "b")]));
  if (!follow) throw new Error("Bootstrap was superseded");
  const selected = createLiveQueryCollection((q) =>
    q.from({ row: store.rows }).where(({ row }) => eq(row.key, "item:a"))
  );
  try {
    await selected.preload();
    const changed = vi.fn();
    const subscription = selected.subscribeChanges(changed);
    await follow({ source: SOURCE, after: H, through: H + 1n, rows: [item("item:b", H + 1n, "changed")], evict: [] });
    await follow({ source: SOURCE, after: H + 1n, through: H + 2n, rows: [], evict: [] });
    expect(changed).not.toHaveBeenCalled();
    await follow({ source: SOURCE, after: H + 2n, through: H + 3n, rows: [item("item:a", H + 3n, "new")], evict: [] });
    expect(changed).toHaveBeenCalledTimes(1);
    expect(selected.toArray[0].value).toEqual({ kind: "item", text: "new" });
    subscription.unsubscribe();
  } finally {
    await selected.cleanup();
  }
});

it("merges delayed history without regressing live items or moving coverage", async () => {
  const store = view();
  const follow = await store.bootstrap(snapshot([item("item:a", H - 1n, "partial")]));
  if (!follow) throw new Error("Bootstrap was superseded");
  const response = deferred<readonly Entity[]>();
  const loading = store.history(response.promise);
  await follow({ source: SOURCE, after: H, through: H + 1n, rows: [item("item:a", H + 1n, "complete")], evict: [] });
  response.resolve([item("item:a", H - 1n, "partial"), item("item:old", H - 100n, "old")]);
  await loading;
  expect(store.rows.get("item:a")?.value).toEqual({ kind: "item", text: "complete" });
  expect(store.rows.has("item:old")).toBe(true);
  expect(store.coverage.revision).toBe(H + 1n);
  const before = Array.from(store.rows.values());
  await expect(store.history(Promise.resolve([item("item:old", H - 100n, "conflict")]))).rejects.toThrow("Conflicting");
  await expect(store.history(Promise.resolve([item("item:future", H + 2n, "future")]))).rejects.toThrow("Invalid");
  expect(Array.from(store.rows.values())).toEqual(before);
});

it("rebootstraps after a long gap and ignores old pages, raw requests, snapshots, and followers", async () => {
  const store = view();
  const oldFollow = await store.bootstrap(snapshot([item("item:a", H, "old")]));
  if (!oldFollow) throw new Error("Bootstrap was superseded");
  const page = deferred<readonly Entity[]>();
  const evidence = deferred<readonly EventEntry[]>();
  const oldSnapshot = deferred<Snapshot>();
  const oldPage = store.history(page.promise);
  const oldRaw = store.raw(evidence.promise);
  const oldBootstrap = store.bootstrap(oldSnapshot.promise);
  const latest = await store.bootstrap(snapshot([item("item:b", H + 1000n, "fresh")], H + 1000n));
  if (!latest) throw new Error("Bootstrap was superseded");
  oldSnapshot.resolve({ source: SOURCE, through: H + 1n, rows: [item("stale-snapshot", H, "stale")] });
  page.resolve([item("stale-page", H, "stale")]);
  evidence.resolve([receipt(H)]);
  await Promise.all([oldBootstrap, oldPage, oldRaw]);
  await oldFollow({
    source: SOURCE,
    after: H,
    through: H + 1n,
    rows: [item("stale-live", H + 1n, "stale")],
    evict: [],
  });
  expect(store.rows.toArray.map((row) => row.key).sort()).toEqual(["$coverage", "item:b"]);
  expect(store.evidence.size).toBe(0);
  expect(store.coverage.revision).toBe(H + 1000n);
  await latest({ source: SOURCE, after: H + 1000n, through: H + 1001n, rows: [], evict: [] });
  expect(store.coverage.revision).toBe(H + 1001n);
});

it("loads exact raw evidence only on demand without publishing conversation changes", async () => {
  const store = view();
  await store.bootstrap(snapshot([item("item:a", H, "assembled")]));
  const changed = vi.fn();
  const subscription = store.rows.subscribeChanges(changed);
  expect(store.evidence.size).toBe(0);
  const native = fromJson(EventEntrySchema, {
    cursor: String(H - 1n),
    origin: { sourceId: SOURCE, sequence: String(H - 1n) },
    event: { native: { line: '{"native":"exact"}' } },
  });
  await store.raw(Promise.resolve([native, receipt(H)]));
  await store.raw(Promise.resolve([native]));
  expect(store.evidence.size).toBe(2);
  const stored = store.evidence.get(`${SOURCE}:${H - 1n}`);
  if (!stored) throw new Error("Missing native frame");
  expect(equals(EventEntrySchema, stored, native)).toBe(true);
  expect(store.coverage.revision).toBe(H);
  expect(changed).not.toHaveBeenCalled();
  await expect(store.raw(Promise.resolve([receipt(H - 1n)]))).rejects.toThrow("Conflicting evidence");
  expect(store.evidence.size).toBe(2);
  subscription.unsubscribe();
});

it("bounds the loaded window across many updates while keeping controls and pending commands", async () => {
  const store = view();
  const control: Entity = { key: "control", revision: H, value: { kind: "control", model: "old" } };
  const pending: Entity = {
    key: "command:model",
    revision: H,
    value: { kind: "command", admission: receipt(H), outcome: null },
  };
  const follow = await store.bootstrap(snapshot([control, pending, item("item:0", H, "0")]));
  if (!follow) throw new Error("Bootstrap was superseded");
  for (let index = 1; index <= 200; index++) {
    const through = H + BigInt(index);
    await follow({
      source: SOURCE,
      after: through - 1n,
      through,
      rows: [item(`item:${index}`, through, String(index))],
      evict: index >= 3 ? [`item:${index - 3}`] : [],
    });
    expect(store.rows.size).toBeLessThanOrEqual(6);
  }
  expect(store.rows.get("control")).toMatchObject(control);
  expect(store.rows.get("command:model")).toMatchObject(pending);
  expect(store.evidence.size).toBe(0);
  await expect(
    follow({ source: SOURCE, after: H + 200n, through: H + 201n, rows: [], evict: ["command:model"] })
  ).rejects.toThrow("Only loaded items");
});

it("rejects a coverage gap and refuses optimistic local mutations", async () => {
  const store = view();
  const follow = await store.bootstrap(snapshot([item("item:a", H, "saved")]));
  if (!follow) throw new Error("Bootstrap was superseded");
  await expect(follow({ source: SOURCE, after: H + 1n, through: H + 2n, rows: [], evict: [] })).rejects.toThrow(
    "rebootstrap required"
  );
  expect(() => store.rows.insert(item("local", H, "not admitted"))).toThrow();
  expect(store.rows.has("local")).toBe(false);
  expect(store.coverage.revision).toBe(H);
});

it("keeps the old view on failed rebootstrap and rejects source replacement or regressed snapshots", async () => {
  const store = view();
  await store.bootstrap(snapshot([item("item:a", H, "saved")]));
  const before = Array.from(store.rows.values());
  await expect(store.bootstrap(Promise.reject(new Error("offline")))).rejects.toThrow("offline");
  expect(store.rows.isReady()).toBe(true);
  await expect(store.bootstrap(Promise.resolve({ source: "other", through: H, rows: [] }))).rejects.toThrow(
    "Source changed"
  );
  await expect(store.bootstrap(snapshot([], H - 1n))).rejects.toThrow("regressed");
  expect(Array.from(store.rows.values())).toEqual(before);
  await store.bootstrap(snapshot([item("item:a", H + 1n, "recovered")], H + 1n));
  expect(store.coverage.revision).toBe(H + 1n);
});

it("makes failed initial sync reject readiness and allows an explicit recovery", async () => {
  const store = view();
  const ready = expect(store.rows.preload()).rejects.toThrow("offline");
  await expect(store.bootstrap(Promise.reject(new Error("offline")))).rejects.toThrow("offline");
  await ready;
  await store.bootstrap(snapshot([item("item:a", H, "recovered")]));
  await store.rows.preload();
  expect(store.rows.isReady()).toBe(true);
  expect(store.coverage.revision).toBe(H);
});
