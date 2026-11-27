import { expect, it } from "vitest";

import { WatchCollection, type CollectionFrame } from "./watch_collection";

type Item = { name: string; uid: string; version: number };
type Frame = CollectionFrame<Item, Item[], Pick<Item, "name" | "uid">>;

function collection(): WatchCollection<Item, Pick<Item, "name" | "uid">> {
  return new WatchCollection({
    kind: "Test item",
    key: (item) => item.name,
    removal: { key: (removed) => removed.name, matches: (held, removed) => held.uid === removed.uid },
    supersedes: (held, incoming) => incoming.version >= held.version,
  });
}

const item = (name: string, uid: string, version: number): Item => ({ name, uid, version });

function completed(items: Item[]): WatchCollection<Item, Pick<Item, "name" | "uid">> {
  const watched = collection();
  watched.apply<Item[]>({ case: "snapshot", value: items }, (chunk) => chunk);
  watched.apply<Item[]>({ case: "snapshotComplete" }, (chunk) => chunk);
  return watched;
}

it.each<[string, Frame]>([
  ["upsert", { case: "upsert", value: item("test-b", "uid-b", 1) }],
  ["removal", { case: "removed", value: { name: "test-a", uid: "uid-a" } }],
])("fails a replay's %s sent before the snapshot completes", (_name, frame) => {
  const watched = collection();
  watched.apply<Item[]>({ case: "snapshot", value: [item("test-a", "uid-a", 1)] }, (chunk) => chunk);
  expect(() => watched.apply(frame, (chunk) => chunk)).toThrow();
});

it("ignores a planned renewal and a removal naming a replaced UID", () => {
  const watched = completed([item("test-a", "uid-a", 1)]);
  expect(watched.apply<Item[]>({ case: "reconnectRequired" }, (chunk) => chunk)).toBe(false);
  expect(watched.apply<Item[]>({ case: "removed", value: { name: "test-a", uid: "uid-old" } }, (chunk) => chunk)).toBe(
    false
  );
  expect(watched.snapshotValues()).toEqual([item("test-a", "uid-a", 1)]);
});

it("replaces only a held item, with a newer version, once the snapshot completed", () => {
  const replaying = collection();
  expect(replaying.replace(item("test-a", "uid-a", 2))).toBe(false);

  const watched = completed([item("test-a", "uid-a", 2)]);
  expect(watched.replace(item("test-unheld", "uid-u", 1))).toBe(false);
  expect(watched.replace(item("test-a", "uid-a", 1))).toBe(false);
  expect(watched.replace(item("test-a", "uid-a", 3))).toBe(true);
  expect(watched.apply<Item[]>({ case: "upsert", value: item("test-a", "uid-a", 2) }, (chunk) => chunk)).toBe(false);
  expect(watched.snapshotValues()).toEqual([item("test-a", "uid-a", 3)]);
});
