// @vitest-environment happy-dom

import { create, equals } from "@bufbuild/protobuf";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import { LocalCommands } from "./local_commands";

const COMMAND = create(CommandSchema, {
  commandId: "input-1",
  operation: { case: "submitInput", value: { text: "  exact text\nsecond line  " } },
});

function admission(cursor = 9n, command: Command = COMMAND): EventEntry {
  return create(EventEntrySchema, {
    cursor,
    origin: { sourceId: "runner", sequence: cursor },
    event: { observation: { case: "commandAdmitted", value: { command } } },
  });
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

it("retains the exact command and immutable Thread target across a new page owner", () => {
  const store = new LocalCommands("sandbox", "session");
  store.remember("thread", COMMAND);
  const restored = new LocalCommands("sandbox", "session").getSnapshot().commands;
  expect(restored).toHaveLength(1);
  expect(restored[0].threadId).toBe("thread");
  expect(equals(CommandSchema, restored[0].command, COMMAND)).toBe(true);
  expect(new LocalCommands("sandbox", "other-session").getSnapshot().commands).toEqual([]);
});

it("retains ahead-of-prefix HTTP admission evidence until matching replay takes ownership", () => {
  const store = new LocalCommands("sandbox", "session");
  store.remember("thread", COMMAND);
  const admitted = admission();
  store.acknowledge(COMMAND, admitted);
  const restored = new LocalCommands("sandbox", "session");
  expect(restored.getSnapshot().commands[0].admission).toEqual(admitted);
  restored.observePrefix([
    create(EventEntrySchema, {
      cursor: 1n,
      origin: { sourceId: "runner", sequence: 1n },
      event: { observation: { case: "harnessStarted", value: {} } },
    }),
  ]);
  expect(restored.getSnapshot().commands).toHaveLength(1);
  restored.observePrefix([admitted]);
  expect(restored.getSnapshot().commands).toEqual([]);
  expect(new LocalCommands("sandbox", "session").getSnapshot().commands).toEqual([]);
});

it("does not resurrect local input when replay wins the race with its HTTP response", () => {
  const store = new LocalCommands("sandbox", "session");
  store.remember("thread", COMMAND);
  store.observePrefix([admission()]);
  store.acknowledge(COMMAND, admission());
  expect(store.getSnapshot().commands).toEqual([]);
  expect(localStorage.length).toBe(0);
});

it("rejects mismatched admission without removing the original input", () => {
  const store = new LocalCommands("sandbox", "session");
  store.remember("thread", COMMAND);
  const changed = create(CommandSchema, {
    commandId: COMMAND.commandId,
    operation: { case: "submitInput", value: { text: "changed" } },
  });
  expect(() => store.acknowledge(COMMAND, admission(9n, changed))).toThrow("does not confirm");
  expect(store.getSnapshot().commands[0].command).toEqual(COMMAND);
  expect(store.getSnapshot().commands[0].admission).toBeNull();
});

it("rejects conflicting HTTP/replay evidence and keeps the saved record inspectable", () => {
  const store = new LocalCommands("sandbox", "session");
  store.remember("thread", COMMAND);
  store.acknowledge(COMMAND, admission());
  expect(() => store.acknowledge(COMMAND, admission(10n))).toThrow("Conflicting");
  expect(() => store.observePrefix([admission(10n)])).toThrow("conflicts");
  expect(new LocalCommands("sandbox", "session").getSnapshot().commands[0].admission).toEqual(admission());
});

it("does not overwrite another tab's distinct pending commands", () => {
  const first = new LocalCommands("sandbox", "session");
  const second = new LocalCommands("sandbox", "session");
  const unsubscribe = first.subscribe(() => {});
  first.remember("thread", COMMAND);
  const model = create(CommandSchema, {
    commandId: "model",
    operation: { case: "changeModel", value: { model: "next" } },
  });
  second.remember("thread", model);
  window.dispatchEvent(new StorageEvent("storage", { key: null }));
  expect(
    first
      .getSnapshot()
      .commands.map((value) => value.command.commandId)
      .sort()
  ).toEqual(["input-1", "model"]);
  expect(new LocalCommands("sandbox", "session").getSnapshot().commands).toHaveLength(2);
  unsubscribe();
});

it("refuses target or payload changes under an existing local command id", () => {
  const store = new LocalCommands("sandbox", "session");
  const existing = store.remember("thread", COMMAND);
  expect(store.remember("thread", COMMAND)).toEqual(existing);
  expect(() => store.remember("other-thread", COMMAND)).toThrow("cannot be reused");
  expect(() =>
    store.remember(
      "thread",
      create(CommandSchema, { commandId: COMMAND.commandId, operation: { case: "stopRunnerSession", value: {} } })
    )
  ).toThrow("cannot be reused");
});

it("fails persistence before a caller can claim submission when storage is full", () => {
  const store = new LocalCommands("sandbox", "session");
  vi.spyOn(Storage.prototype, "setItem").mockImplementationOnce(() => {
    throw new DOMException("storage full", "QuotaExceededError");
  });
  expect(() => store.remember("thread", COMMAND)).toThrow("storage full");
  expect(store.getSnapshot().commands).toEqual([]);
});

it("preserves undecodable stored data and reports the problem instead of starting empty", () => {
  const store = new LocalCommands("sandbox", "session");
  store.remember("thread", COMMAND);
  const key = localStorage.key(0);
  if (key === null) throw new Error("Missing stored command");
  localStorage.setItem(key, "broken data");
  const restored = new LocalCommands("sandbox", "session");
  expect(restored.getSnapshot().error).not.toBeNull();
  expect(() => restored.remember("thread", COMMAND)).toThrow();
  expect(localStorage.getItem(key)).toBe("broken data");
});
