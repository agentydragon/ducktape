// @vitest-environment happy-dom

import { create, equals } from "@bufbuild/protobuf";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import { LocalCommands, MAX_RETAINED_COMMANDS } from "./local_commands";

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
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  const restored = new LocalCommands("thread").getSnapshot().commands;
  expect(restored).toHaveLength(1);
  expect(equals(CommandSchema, restored[0].command, COMMAND)).toBe(true);
  expect(new LocalCommands("other-thread").getSnapshot().commands).toEqual([]);
});

it("retains ahead-of-prefix HTTP admission evidence until matching replay takes ownership", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  const admitted = admission();
  store.acknowledge(COMMAND, admitted);
  const restored = new LocalCommands("thread");
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
  expect(new LocalCommands("thread").getSnapshot().commands).toEqual([]);
});

it("does not resurrect local input when replay wins the race with its HTTP response", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  store.observePrefix([admission()]);
  store.acknowledge(COMMAND, admission());
  expect(store.getSnapshot().commands).toEqual([]);
  expect(localStorage.length).toBe(0);
});

it("rejects mismatched admission without removing the original input", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  const changed = create(CommandSchema, {
    commandId: COMMAND.commandId,
    operation: { case: "submitInput", value: { text: "changed" } },
  });
  expect(() => store.acknowledge(COMMAND, admission(9n, changed))).toThrow("does not confirm");
  expect(store.getSnapshot().commands[0].command).toEqual(COMMAND);
  expect(store.getSnapshot().commands[0].admission).toBeNull();
});

it("rejects conflicting HTTP/replay evidence and keeps the saved record inspectable", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  store.acknowledge(COMMAND, admission());
  expect(() => store.acknowledge(COMMAND, admission(10n))).toThrow("Conflicting");
  expect(() => store.observePrefix([admission(10n)])).toThrow("Conflicting");
  expect(new LocalCommands("thread").getSnapshot().commands[0].admission).toEqual(admission());
});

it("does not overwrite another tab's distinct pending commands", () => {
  const first = new LocalCommands("thread");
  const second = new LocalCommands("thread");
  const unsubscribe = first.subscribe(() => {});
  first.remember(COMMAND);
  const model = create(CommandSchema, {
    commandId: "model",
    operation: { case: "changeModel", value: { model: "next" } },
  });
  second.remember(model);
  window.dispatchEvent(new StorageEvent("storage", { key: null }));
  expect(
    first
      .getSnapshot()
      .commands.map((value) => value.command.commandId)
      .sort()
  ).toEqual(["input-1", "model"]);
  expect(new LocalCommands("thread").getSnapshot().commands).toHaveLength(2);
  unsubscribe();
});

it("refuses payload changes under an existing local command id", () => {
  const store = new LocalCommands("thread");
  const existing = store.remember(COMMAND);
  expect(store.remember(COMMAND)).toEqual(existing);
  expect(() =>
    store.remember(
      create(CommandSchema, { commandId: COMMAND.commandId, operation: { case: "stopRunnerSession", value: {} } })
    )
  ).toThrow("cannot be reused");
});

it("fails persistence before a caller can claim submission when storage is full", () => {
  const store = new LocalCommands("thread");
  vi.spyOn(localStorage, "setItem").mockImplementationOnce(() => {
    throw new DOMException("storage full", "QuotaExceededError");
  });
  expect(() => store.remember(COMMAND)).toThrow("storage full");
  expect(store.getSnapshot().commands).toEqual([]);
});

it("refuses to retain more commands than the bounded server selection can reconcile", () => {
  const store = new LocalCommands("thread");
  for (let index = 0; index < MAX_RETAINED_COMMANDS; index += 1) {
    store.remember(
      create(CommandSchema, {
        commandId: `command-${index}`,
        operation: { case: "stopRunnerSession", value: {} },
      })
    );
  }
  expect(() =>
    store.remember(
      create(CommandSchema, {
        commandId: "one-too-many",
        operation: { case: "stopRunnerSession", value: {} },
      })
    )
  ).toThrow(`maximum ${MAX_RETAINED_COMMANDS}`);
  expect(store.getSnapshot().commands).toHaveLength(MAX_RETAINED_COMMANDS);
});

it("preserves undecodable stored data and reports the problem instead of starting empty", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  const key = localStorage.key(0);
  if (key === null) throw new Error("Missing stored command");
  localStorage.setItem(key, "broken data");
  const restored = new LocalCommands("thread");
  expect(restored.getSnapshot().error).not.toBeNull();
  expect(() => restored.remember(COMMAND)).toThrow();
  expect(localStorage.getItem(key)).toBe("broken data");
});

it("keeps observed admission visible if persisting its receipt fails", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  vi.spyOn(localStorage, "setItem").mockImplementationOnce(() => {
    throw new DOMException("receipt storage full", "QuotaExceededError");
  });
  store.acknowledge(COMMAND, admission());
  expect(store.getSnapshot().commands[0].admission).toEqual(admission());
  expect(store.getSnapshot().error).toContain("receipt storage full");
  expect(() => store.acknowledge(COMMAND, admission(10n))).toThrow("Conflicting");
  expect(() => store.observePrefix([admission(10n)])).toThrow("Conflicting");
  const unsubscribe = store.subscribe(() => {});
  window.dispatchEvent(new StorageEvent("storage", { key: null }));
  expect(store.getSnapshot().commands[0].admission).toEqual(admission());
  store.observePrefix([admission()]);
  expect(store.getSnapshot().commands).toEqual([]);
  unsubscribe();
});
