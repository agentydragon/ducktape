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
  InterruptTurnResponseSchema,
  type Dispatch,
} from "../../../runner_gateway/dispatcher/session_dispatcher_pb";

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

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

it("retains the exact command and immutable Thread target across a new page owner", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  const restored = new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands;
  expect(restored).toHaveLength(1);
  expect(equals(CommandSchema, create(CommandSchema, restored[0].command), COMMAND)).toBe(true);
  expect(new LocalCommands("other-thread", COMMAND_TARGET).getSnapshot().commands).toEqual([]);
});

it("pins the original Sandbox UID and rejects a changed destination for the same command ID", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  const replacement = { sandbox: COMMAND_TARGET.sandbox, sandboxUid: "replacement-uid" };
  const reopened = new LocalCommands("thread", replacement);
  expect(reopened.getSnapshot().commands[0]?.target).toEqual(COMMAND_TARGET);
  expect(() => reopened.remember(COMMAND)).toThrow("different Sandbox destination");
  expect(() =>
    store.acknowledgeDispatch(
      COMMAND,
      create(DispatchSchema, {
        ...dispatch(COMMAND, DeliveryState.QUEUED_IN_DISPATCHER, "2026-10-14T12:00:00Z"),
        sandboxUid: replacement.sandboxUid,
      })
    )
  ).toThrow("different Sandbox destination");
});

it("keeps legacy commands without a target for History reconciliation but cannot bind a new UID", () => {
  localStorage.setItem(
    "agentplane.pending:thread:legacy",
    JSON.stringify({
      submittedAt: 1,
      attempted: true,
      command: { commandId: "legacy", operation: { case: "submitInput", value: { text: "saved" } } },
      admission: null,
      dispatch: null,
    })
  );
  const store = new LocalCommands("thread", COMMAND_TARGET);
  const legacy = store.getSnapshot().commands[0];
  expect(legacy.target).toBeNull();
  expect(store.remember(legacy.command).target).toBeNull();
  store.observePrefix([admission(9n, create(CommandSchema, legacy.command))]);
  expect(store.getSnapshot().commands).toEqual([]);
});

it("retains ahead-of-prefix HTTP admission evidence until matching replay takes ownership", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  const admitted = admission();
  store.acknowledge(COMMAND, admitted);
  const restored = new LocalCommands("thread", COMMAND_TARGET);
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
  expect(restored.getSnapshot().inputEchoes).toEqual([
    { commandId: COMMAND.commandId, text: "  exact text\nsecond line  " },
  ]);
  expect(new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands).toEqual([]);
});

it("retains an immediate interrupt result without recording a durable Dispatch", () => {
  const interrupted = create(CommandSchema, {
    commandId: "interrupt-1",
    operation: { case: "interruptTurn", value: { turnId: "turn-1" } },
  });
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(interrupted);
  store.acknowledgeInterrupt(
    interrupted,
    fromJson(InterruptTurnResponseSchema, {
      state: "DELIVERY_STATE_NO_OP",
      reason: "turn already ended",
    })
  );

  const retained = new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands[0];
  expect(retained.dispatch).toBeNull();
  expect(retained.interruptResult?.state).toBe(DeliveryState.NO_OP);
  expect(retained.interruptResult?.reason).toBe("turn already ended");
  expect(retained.interruptResult?.admission).toBeUndefined();
  expect(store.markAttempted(interrupted.commandId)).toBe(false);
});

it("does not resurrect local input when replay wins the race with its HTTP response", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  store.observePrefix([admission()]);
  store.acknowledge(COMMAND, admission());
  expect(store.getSnapshot().commands).toEqual([]);
  expect(store.getSnapshot().inputEchoes).toEqual([
    { commandId: COMMAND.commandId, text: "  exact text\nsecond line  " },
  ]);
  expect(localStorage.length).toBe(1);
});

it("keeps a projected local input available until its echo body is fetched", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  store.observeCommandIds(new Set([COMMAND.commandId]));

  expect(store.getSnapshot().commands).toEqual([]);
  expect(store.getSnapshot().inputEchoes).toEqual([
    { commandId: COMMAND.commandId, text: "  exact text\nsecond line  " },
  ]);
  expect(new LocalCommands("thread", COMMAND_TARGET).getSnapshot().inputEchoes).toEqual([
    { commandId: COMMAND.commandId, text: "  exact text\nsecond line  " },
  ]);

  store.acknowledgeEcho(COMMAND.commandId);
  expect(store.getSnapshot().inputEchoes).toEqual([]);
  expect(localStorage.length).toBe(0);
});

it("rejects mismatched admission without removing the original input", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  const changed = create(CommandSchema, {
    commandId: COMMAND.commandId,
    operation: { case: "submitInput", value: { text: "changed" } },
  });
  expect(() => store.acknowledge(COMMAND, admission(9n, changed))).toThrow("does not confirm");
  expect(create(CommandSchema, store.getSnapshot().commands[0].command)).toEqual(COMMAND);
  expect(store.getSnapshot().commands[0].admission).toBeNull();
});

it("rejects conflicting HTTP/replay evidence and keeps the saved record inspectable", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  store.acknowledge(COMMAND, admission());
  expect(() => store.acknowledge(COMMAND, admission(10n))).toThrow("Conflicting");
  expect(() => store.observePrefix([admission(10n)])).toThrow("Conflicting");
  expect(new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands[0].admission).toEqual(admission());
});

it("does not overwrite another tab's distinct pending commands", () => {
  const first = new LocalCommands("thread", COMMAND_TARGET);
  const second = new LocalCommands("thread", COMMAND_TARGET);
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
  expect(new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands).toHaveLength(2);
  unsubscribe();
});

it("only cancels a command that has never been attempted", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  expect(new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands[0].attempted).toBe(false);
  expect(store.cancelUnsent(COMMAND.commandId)).toBe(true);
  expect(new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands).toEqual([]);
  store.remember(COMMAND);
  expect(store.markAttempted(COMMAND.commandId)).toBe(true);
  expect(new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands[0].attempted).toBe(true);
  expect(store.cancelUnsent(COMMAND.commandId)).toBe(false);
  expect(new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands).toHaveLength(1);
});

it("treats commands stored before the attempted flag existed as already sent", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  const key = localStorage.key(0)!;
  const record = JSON.parse(localStorage.getItem(key)!);
  delete record.attempted;
  localStorage.setItem(key, JSON.stringify(record));
  const restored = new LocalCommands("thread", COMMAND_TARGET);
  expect(restored.getSnapshot().commands[0].attempted).toBe(true);
  expect(restored.cancelUnsent(COMMAND.commandId)).toBe(false);
});

it("refuses payload changes under an existing local command id", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
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
  const store = new LocalCommands("thread", COMMAND_TARGET);
  vi.spyOn(localStorage, "setItem").mockImplementationOnce(() => {
    throw new DOMException("storage full", "QuotaExceededError");
  });
  expect(() => store.remember(COMMAND)).toThrow("storage full");
  expect(store.getSnapshot().commands).toEqual([]);
});

it("refuses to retain more commands than the bounded server selection can reconcile", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
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
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  const key = localStorage.key(0);
  if (key === null) throw new Error("Missing stored command");
  localStorage.setItem(key, "broken data");
  const restored = new LocalCommands("thread", COMMAND_TARGET);
  expect(restored.getSnapshot().error).not.toBeNull();
  expect(() => restored.remember(COMMAND)).toThrow();
  expect(localStorage.getItem(key)).toBe("broken data");
});

it("keeps observed admission visible if persisting its receipt fails", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
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

it("persists Dispatcher queue state and accepts a newer retry transition without regressing on stale reads", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  store.markAttempted(COMMAND.commandId);
  store.acknowledgeDispatch(COMMAND, dispatch(COMMAND, DeliveryState.DELIVERING_TO_RUNNER, "2026-10-14T12:00:02Z"));
  const restored = new LocalCommands("thread", COMMAND_TARGET);
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

it("checks admission included in a Dispatcher receipt and transfers the input to History replay", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  const admitted = admission();
  store.acknowledgeDispatch(
    COMMAND,
    dispatch(COMMAND, DeliveryState.RUNNER_ADMITTED, "2026-10-14T12:00:02Z", admitted)
  );
  expect(new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands[0].admission).toEqual(admitted);
  store.observePrefix([admitted]);
  expect(store.getSnapshot().commands).toEqual([]);
  expect(store.getSnapshot().inputEchoes).toEqual([
    { commandId: COMMAND.commandId, text: "  exact text\nsecond line  " },
  ]);
});

it("roundtrips an UNKNOWN Dispatcher state through local ProtoJSON persistence", () => {
  const store = new LocalCommands("thread", COMMAND_TARGET);
  store.remember(COMMAND);
  store.acknowledgeDispatch(COMMAND, dispatch(COMMAND, DeliveryState.UNKNOWN, "2026-10-14T12:00:02Z"));

  const restored = new LocalCommands("thread", COMMAND_TARGET).getSnapshot().commands;
  expect(restored).toHaveLength(1);
  expect(restored[0].command.commandId).toBe(COMMAND.commandId);
  expect(restored[0].dispatch?.state).toBe(DeliveryState.UNKNOWN);
});

it("reads Dispatch and interrupt evidence saved in the previous local format", () => {
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
        state: "DELIVERY_STATE_NO_OP",
        reason: "turn already ended",
        admission: null,
      },
    })
  );

  const restored = new LocalCommands("thread", COMMAND_TARGET).getSnapshot();
  expect(restored.error).toBeNull();
  expect(restored.commands).toHaveLength(2);
  const savedInput = restored.commands.find((value) => value.command.commandId === input.commandId);
  expect(savedInput?.command).toEqual(input);
  expect(savedInput?.dispatch?.state).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  expect(savedInput?.dispatch?.updatedAt?.seconds).toBe(BigInt(Date.parse("2026-10-14T12:00:02Z") / 1000));
  expect(savedInput?.dispatch?.admission).toBeUndefined();

  const savedInterrupt = restored.commands.find((value) => value.command.commandId === interrupt.commandId);
  expect(savedInterrupt?.command).toEqual(interrupt);
  expect(savedInterrupt?.interruptResult?.state).toBe(DeliveryState.NO_OP);
  expect(savedInterrupt?.interruptResult?.admission).toBeUndefined();
});
