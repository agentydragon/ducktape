// @vitest-environment happy-dom

import { create, equals, fromJson } from "@bufbuild/protobuf";
import { TimestampSchema } from "@bufbuild/protobuf/wkt";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../../proto/command_pb";
import { EventEntrySchema, type EventEntry } from "../../../proto/event_log_pb";
import { LocalCommands, MAX_RETAINED_COMMANDS } from "./local_commands";
import {
  DeliveryState,
  DispatchSchema,
  type Dispatch,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";

const COMMAND = create(CommandSchema, {
  commandId: "input-1",
  operation: { case: "submitInput", value: { text: "  exact text\nsecond line  " } },
});
const COMMAND_TARGET = { sandbox: "box", sandboxUid: "20000000-0000-4000-8000-000000000001" };

function admission(cursor = 9n, command: Command = COMMAND): EventEntry {
  return create(EventEntrySchema, {
    cursor,
    origin: { sourceId: "runner", sequence: cursor },
    event: { observation: { case: "commandAdmitted", value: { command } } },
  });
}

function dispatch(
  command: Command,
  state: DeliveryState,
  updatedAt: string,
  event: EventEntry | null = null
): Dispatch {
  return create(DispatchSchema, {
    commandId: command.commandId,
    sessionId: "thread",
    ...COMMAND_TARGET,
    state,
    updatedAt: fromJson(TimestampSchema, updatedAt),
    ...(event ? { admission: event } : {}),
  });
}

function admittedDispatch(event = admission()): Dispatch {
  return dispatch(COMMAND, DeliveryState.RUNNER_ADMITTED, "2026-10-14T12:00:02Z", event);
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

it("retains a new Session-addressed command without a Sandbox destination", () => {
  const saved = new LocalCommands("thread").remember(COMMAND);
  expect(saved.destination).toEqual({ kind: "session" });
  // Cached older tabs still read the same Session-addressed storage shape.
  expect(JSON.parse(localStorage.getItem("agentplane.pending:thread:input-1")!)).toMatchObject({
    sessionAddressed: true,
    target: null,
  });
  const restored = new LocalCommands("thread").getSnapshot().commands[0];
  expect(restored.destination).toEqual({ kind: "session" });
  expect(equals(CommandSchema, create(CommandSchema, restored.command), COMMAND)).toBe(true);
});

it("retains the exact command in its Session across a new page owner", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  const restored = new LocalCommands("thread").getSnapshot().commands;
  expect(restored).toHaveLength(1);
  expect(equals(CommandSchema, create(CommandSchema, restored[0].command), COMMAND)).toBe(true);
  expect(new LocalCommands("other-thread").getSnapshot().commands).toEqual([]);
});

it("persists a watch cursor under the canonical Session and command filter", () => {
  const store = new LocalCommands("thread");
  expect(store.saveDispatchWatchResumeToken("thread", ["b", "a", "a"], "opaque-cursor")).toBe(true);

  const restored = new LocalCommands("thread");
  expect(restored.dispatchWatchResumeToken("thread", ["a", "b"])).toBe("opaque-cursor");
  expect(restored.dispatchWatchResumeToken("thread", ["a"])).toBeNull();
  expect(new LocalCommands("other-thread").dispatchWatchResumeToken("other-thread", ["a", "b"])).toBeNull();
  expect(() => restored.dispatchWatchResumeToken("other-thread", ["a", "b"])).toThrow("different Session");
});

it("leaves a cursor absent when local storage cannot persist it", () => {
  const store = new LocalCommands("thread");
  vi.spyOn(localStorage, "setItem").mockImplementationOnce(() => {
    throw new DOMException("storage full", "QuotaExceededError");
  });

  expect(store.saveDispatchWatchResumeToken("thread", ["a"], "opaque-cursor")).toBe(false);
  expect(new LocalCommands("thread").dispatchWatchResumeToken("thread", ["a"])).toBeNull();
});

it("keeps lost deletion coverage visible across receipt storage failures and reloads", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  store.recordDispatchWatchCoverageLoss();
  vi.spyOn(localStorage, "setItem").mockImplementationOnce(() => {
    throw new DOMException("storage full", "QuotaExceededError");
  });

  expect(store.acknowledgeDispatch(COMMAND, dispatch(COMMAND, DeliveryState.FAILED, "2026-10-14T12:00:02Z"))).toBe(
    false
  );
  expect(store.getSnapshot().dispatchWatchCoverageLost).toBe(true);
  expect(store.getSnapshot().error).toContain("storage full");

  const unsubscribe = store.subscribe(() => {});
  expect(store.getSnapshot().dispatchWatchCoverageLost).toBe(true);
  unsubscribe();
  expect(new LocalCommands("thread").getSnapshot().dispatchWatchCoverageLost).toBe(true);
});

it("reconciles a legacy Sandbox command only against its original destination", () => {
  const key = "agentplane.pending:thread:input-1";
  const saved = JSON.stringify({ command: COMMAND, submittedAt: 42, target: COMMAND_TARGET, admission: null });
  localStorage.setItem(key, saved);
  const store = new LocalCommands("thread");
  expect(store.remember(COMMAND).destination).toEqual({ kind: "reconcile-only", target: COMMAND_TARGET });
  expect(store.markAttempted(COMMAND.commandId)).toBe(false);
  expect(localStorage.getItem(key)).toBe(saved);
  const replacement = { sandbox: COMMAND_TARGET.sandbox, sandboxUid: "replacement-uid" };
  const queued = dispatch(COMMAND, DeliveryState.QUEUED_IN_DISPATCHER, "2026-10-14T12:00:00Z");
  expect(() =>
    store.acknowledgeDispatch(COMMAND, create(DispatchSchema, { ...queued, sandboxUid: replacement.sandboxUid }))
  ).toThrow("different Sandbox destination");
  expect(store.acknowledgeDispatch(COMMAND, queued)).toBe(true);
  expect(new LocalCommands("thread").getSnapshot().commands[0]).toMatchObject({
    destination: { kind: "reconcile-only", target: COMMAND_TARGET },
    dispatch: queued,
  });
  expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({ target: COMMAND_TARGET });
});

it("keeps an unscoped ProtoJSON command's exact input until History confirms it", () => {
  const text = "  exact legacy text\nsecond line  ";
  localStorage.setItem(
    "agentplane.pending:thread:legacy",
    JSON.stringify({
      submittedAt: 1,
      command: { commandId: "legacy", submitInput: { text } },
      admission: null,
    })
  );
  const store = new LocalCommands("thread");
  const legacy = store.getSnapshot().commands[0];
  expect(legacy.destination).toEqual({ kind: "reconcile-only", target: null });
  expect(legacy.attempted).toBe(true);
  expect(store.markAttempted("legacy")).toBe(false);
  store.observeCommandIds(new Set(["legacy"]));
  expect(store.getSnapshot().commands).toEqual([]);
  expect(new LocalCommands("thread").getSnapshot().inputEchoes).toEqual([{ commandId: "legacy", text }]);
});

it("keeps a queued ProtoJSON command readable by the writing tab after a watch update", () => {
  const key = "agentplane.pending:thread:legacy";
  const command = { commandId: "legacy", submitInput: { text: "  original\ninput  " } };
  const queued = { commandId: "legacy", sessionId: "thread", state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER" };
  localStorage.setItem(key, JSON.stringify({ command, admission: null, submittedAt: 1, dispatch: queued }));

  const store = new LocalCommands("thread");
  const failed = fromJson(DispatchSchema, { ...queued, command, state: "DELIVERY_STATE_FAILED" });
  expect(store.updateDispatch("legacy", failed)).toBe(true);
  const saved = JSON.parse(localStorage.getItem(key)!);
  expect(fromJson(CommandSchema, saved.command)).toEqual(fromJson(CommandSchema, command));
  expect(saved.dispatch).not.toHaveProperty("command");
  expect(fromJson(DispatchSchema, saved.dispatch)).toEqual({ ...failed, command: undefined });
});

it("does not resurrect local input when replay wins the race with its HTTP response", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  store.observeCommandIds(new Set([COMMAND.commandId]));
  store.acknowledgeDispatch(COMMAND, admittedDispatch());
  expect(store.getSnapshot().commands).toEqual([]);
  expect(store.getSnapshot().inputEchoes).toEqual([
    { commandId: COMMAND.commandId, text: "  exact text\nsecond line  " },
  ]);
  expect(localStorage.length).toBe(1);
});

it("keeps a projected local input available until its echo body is fetched", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  store.observeCommandIds(new Set([COMMAND.commandId]));

  expect(store.getSnapshot().commands).toEqual([]);
  expect(store.getSnapshot().inputEchoes).toEqual([
    { commandId: COMMAND.commandId, text: "  exact text\nsecond line  " },
  ]);
  expect(new LocalCommands("thread").getSnapshot().inputEchoes).toEqual([
    { commandId: COMMAND.commandId, text: "  exact text\nsecond line  " },
  ]);

  store.acknowledgeEcho(COMMAND.commandId);
  expect(store.getSnapshot().inputEchoes).toEqual([]);
  expect(localStorage.length).toBe(0);
});

it("rejects mismatched admission without removing the original input", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  const changed = create(CommandSchema, {
    commandId: COMMAND.commandId,
    operation: { case: "submitInput", value: { text: "changed" } },
  });
  expect(() => store.acknowledgeDispatch(COMMAND, admittedDispatch(admission(9n, changed)))).toThrow(
    "does not confirm"
  );
  expect(create(CommandSchema, store.getSnapshot().commands[0].command)).toEqual(COMMAND);
  expect(store.getSnapshot().commands[0].admission).toBeNull();
});

it("rejects conflicting Dispatcher admissions and keeps the saved record inspectable", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  store.acknowledgeDispatch(COMMAND, admittedDispatch());
  expect(() => store.acknowledgeDispatch(COMMAND, admittedDispatch(admission(10n)))).toThrow("Conflicting");
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

it("keeps a legacy command from an older tab alongside a new Session command", () => {
  const store = new LocalCommands("thread");
  const unsubscribe = store.subscribe(() => {});
  store.remember(COMMAND);
  const key = "agentplane.pending:thread:old-tab";
  const oldRecord = JSON.stringify({
    command: { ...COMMAND, commandId: "old-tab" },
    submittedAt: 1,
    attempted: false,
    sessionAddressed: true,
    target: COMMAND_TARGET,
    admission: null,
  });
  localStorage.setItem(key, oldRecord);
  window.dispatchEvent(new StorageEvent("storage", { key }));

  expect(store.getSnapshot().commands.map((value) => value.destination)).toEqual([
    { kind: "reconcile-only", target: COMMAND_TARGET },
    { kind: "session" },
  ]);
  expect(store.markAttempted("old-tab")).toBe(false);
  expect(store.cancelUnsent("old-tab")).toBe(false);
  expect(localStorage.getItem(key)).toBe(oldRecord);
  unsubscribe();
});

it("only cancels a command that has never been attempted", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  expect(new LocalCommands("thread").getSnapshot().commands[0].attempted).toBe(false);
  expect(store.cancelUnsent(COMMAND.commandId)).toBe(true);
  expect(new LocalCommands("thread").getSnapshot().commands).toEqual([]);
  store.remember(COMMAND);
  expect(store.markAttempted(COMMAND.commandId)).toBe(true);
  expect(store.markAttempted(COMMAND.commandId)).toBe(false);
  expect(new LocalCommands("thread").getSnapshot().commands[0].attempted).toBe(true);
  expect(store.cancelUnsent(COMMAND.commandId)).toBe(false);
  expect(new LocalCommands("thread").getSnapshot().commands).toHaveLength(1);
});

it("treats commands stored before the attempted flag existed as already sent", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  const key = localStorage.key(0)!;
  const record = JSON.parse(localStorage.getItem(key)!);
  delete record.attempted;
  localStorage.setItem(key, JSON.stringify(record));
  const restored = new LocalCommands("thread");
  expect(restored.getSnapshot().commands[0].attempted).toBe(true);
  expect(restored.cancelUnsent(COMMAND.commandId)).toBe(false);
});

it("refuses payload changes under an existing local command id", () => {
  const store = new LocalCommands("thread");
  const existing = store.remember(COMMAND);
  expect(store.remember(COMMAND)).toEqual(existing);
  expect(() =>
    store.remember(
      create(CommandSchema, {
        commandId: COMMAND.commandId,
        operation: { case: "changeModel", value: { model: "other-model" } },
      })
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
        operation: { case: "changeModel", value: { model: `model-${index}` } },
      })
    );
  }
  expect(() =>
    store.remember(
      create(CommandSchema, {
        commandId: "one-too-many",
        operation: { case: "changeModel", value: { model: "last-model" } },
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
  store.acknowledgeDispatch(COMMAND, admittedDispatch());
  expect(store.getSnapshot().commands[0].admission).toEqual(admission());
  expect(store.getSnapshot().error).toContain("receipt storage full");
  expect(() => store.acknowledgeDispatch(COMMAND, admittedDispatch(admission(10n)))).toThrow("Conflicting");
  const unsubscribe = store.subscribe(() => {});
  window.dispatchEvent(new StorageEvent("storage", { key: null }));
  expect(store.getSnapshot().commands[0].admission).toEqual(admission());
  store.observeCommandIds(new Set([COMMAND.commandId]));
  expect(store.getSnapshot().commands).toEqual([]);
  unsubscribe();
});

it("persists Dispatcher queue state and accepts a newer retry transition without regressing on stale reads", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  store.markAttempted(COMMAND.commandId);
  store.acknowledgeDispatch(COMMAND, dispatch(COMMAND, DeliveryState.DELIVERING_TO_RUNNER, "2026-10-14T12:00:02Z"));
  const restored = new LocalCommands("thread");
  expect(restored.getSnapshot().commands[0].dispatch?.state).toBe(DeliveryState.DELIVERING_TO_RUNNER);

  restored.updateDispatch(
    COMMAND.commandId,
    dispatch(COMMAND, DeliveryState.QUEUED_IN_DISPATCHER, "2026-10-14T12:00:03Z")
  );
  expect(restored.getSnapshot().commands[0].dispatch?.state).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  restored.updateDispatch(
    COMMAND.commandId,
    dispatch(COMMAND, DeliveryState.DELIVERING_TO_RUNNER, "2026-10-14T12:00:01Z")
  );
  expect(restored.getSnapshot().commands[0].dispatch?.state).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
});

it("keeps a Dispatcher admission until History projects its command", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  const admitted = admission();
  store.acknowledgeDispatch(COMMAND, admittedDispatch(admitted));
  const restored = new LocalCommands("thread");
  restored.observeCommandIds(new Set(["another-command"]));
  expect(restored.getSnapshot().commands[0].admission).toEqual(admitted);
  restored.observeCommandIds(new Set([COMMAND.commandId]));
  expect(restored.getSnapshot().commands).toEqual([]);
  expect(restored.getSnapshot().inputEchoes).toEqual([
    { commandId: COMMAND.commandId, text: "  exact text\nsecond line  " },
  ]);
});

it("roundtrips an UNKNOWN Dispatcher state through local ProtoJSON persistence", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  store.acknowledgeDispatch(COMMAND, dispatch(COMMAND, DeliveryState.UNKNOWN, "2026-10-14T12:00:02Z"));

  const restored = new LocalCommands("thread").getSnapshot().commands;
  expect(restored).toHaveLength(1);
  expect(restored[0].command.commandId).toBe(COMMAND.commandId);
  expect(restored[0].dispatch?.state).toBe(DeliveryState.UNKNOWN);
});

it("maps an unrecognized enum in saved Dispatch evidence to unknown", () => {
  const input = {
    commandId: "legacy-input",
    operation: { case: "submitInput", value: { text: "keep the exact legacy input" } },
  };
  const interrupt = {
    commandId: "legacy-interrupt",
    operation: { case: "interruptTurn", value: { turnId: "turn-legacy" } },
  };
  localStorage.setItem(
    `agentplane.pending:${encodeURIComponent("thread")}:${input.commandId}`,
    JSON.stringify({
      submittedAt: 1,
      attempted: true,
      command: input,
      admission: null,
      dispatch: {
        commandId: input.commandId,
        sessionId: "thread",
        state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
        reason: "sandbox_resuming",
        updatedAt: "2026-10-14T12:00:02Z",
        admission: null,
      },
      interruptResult: null,
    })
  );
  localStorage.setItem(
    `agentplane.pending:${encodeURIComponent("thread")}:${interrupt.commandId}`,
    JSON.stringify({
      submittedAt: 2,
      attempted: true,
      command: interrupt,
      admission: null,
      dispatch: null,
      interruptResult: {
        state: "DELIVERY_STATE_FUTURE",
        reason: "turn already ended",
        admission: null,
      },
    })
  );

  const restored = new LocalCommands("thread").getSnapshot();
  expect(restored.error).toBeNull();
  expect(restored.commands).toHaveLength(2);
  const savedInput = restored.commands.find((value) => value.command.commandId === input.commandId);
  expect(savedInput?.command).toEqual(input);
  expect(savedInput?.dispatch?.state).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  expect(savedInput?.dispatch?.updatedAt?.seconds).toBe(BigInt(Date.parse("2026-10-14T12:00:02Z") / 1000));
  expect(savedInput?.dispatch?.admission).toBeUndefined();

  const savedInterrupt = restored.commands.find((value) => value.command.commandId === interrupt.commandId);
  expect(savedInterrupt?.command).toEqual(interrupt);
  expect(savedInterrupt?.interruptResult?.state).toBe(DeliveryState.UNKNOWN);
  expect(savedInterrupt?.interruptResult?.admission).toBeUndefined();
});

it("normalizes an unrecognized Dispatcher enum number before retaining its receipt", () => {
  const store = new LocalCommands("thread");
  store.remember(COMMAND);
  store.acknowledgeDispatch(COMMAND, dispatch(COMMAND, 99 as DeliveryState, "2026-10-14T12:00:02Z"));

  expect(store.getSnapshot().commands[0].dispatch?.state).toBe(DeliveryState.UNKNOWN);
});
