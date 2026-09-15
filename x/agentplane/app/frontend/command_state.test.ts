import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { expect, it } from "vitest";

import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";
import { EventSchema, TurnStatus } from "../../protocol/event_pb";
import { observedCommands } from "./command_state";

function entry(cursor: bigint, observation: MessageInitShape<typeof EventSchema>["observation"]): EventEntry {
  return create(EventEntrySchema, { cursor, origin: { sourceId: "runner", sequence: cursor }, event: { observation } });
}

function admission(cursor: bigint, command: Command): EventEntry {
  return entry(cursor, { case: "commandAdmitted", value: { command } });
}

const MESSAGE = create(CommandSchema, {
  commandId: "message",
  operation: { case: "submitInput", value: { text: "A" } },
});
const MODEL = create(CommandSchema, {
  commandId: "model",
  operation: { case: "changeModel", value: { model: "next" } },
});
const INTERRUPT = create(CommandSchema, {
  commandId: "interrupt",
  operation: { case: "interruptTurn", value: { turnId: "turn" } },
});
const STOP = create(CommandSchema, { commandId: "stop", operation: { case: "stopRunnerSession", value: {} } });

it("keeps all command kinds pending until their own causal outcome", () => {
  const entries = [admission(1n, MESSAGE), admission(2n, MODEL), admission(3n, INTERRUPT), admission(4n, STOP)];
  expect(observedCommands(entries).filter((command) => command.outcome === null)).toHaveLength(4);
  entries.push(entry(5n, { case: "modelChanged", value: { commandId: "model", model: "next" } }));
  let commands = observedCommands(entries);
  expect(commands.filter((command) => command.outcome === null).map((command) => command.command.commandId)).toEqual([
    "message",
    "interrupt",
    "stop",
  ]);
  expect(commands[1].outcome).toBe(entries[4]);
  entries.push(
    entry(6n, {
      case: "turnCompleted",
      value: { turnId: "turn", status: TurnStatus.INTERRUPTED, interruptedByCommandId: "interrupt" },
    })
  );
  entries.push(entry(7n, { case: "harnessExited", value: { stoppedByCommandId: "stop", stoppedByRunner: true } }));
  commands = observedCommands(entries);
  expect(commands.filter((command) => command.outcome === null).map((command) => command.command.commandId)).toEqual([
    "message",
  ]);
  expect(commands[0].command).toBe(MESSAGE);
  expect(commands[0].admission).toBe(entries[0]);
});

it("settles every originating command of one coalesced native message, retaining the native evidence", () => {
  const second = create(CommandSchema, {
    commandId: "second",
    operation: { case: "submitInput", value: { text: "B" } },
  });
  const confirmation = entry(3n, {
    case: "harnessUserMessageConfirmed",
    value: { harnessMessageId: "native", originCommandIds: ["message", "second"], text: "A\nB", turnId: "turn" },
  });
  const entries = [admission(1n, MESSAGE), admission(2n, second), confirmation];
  const commands = observedCommands(entries);
  expect(commands.map((command) => command.outcome)).toEqual([confirmation, confirmation]);
  expect(commands[0].command.operation).toEqual(MESSAGE.operation);
  expect(observedCommands(entries)).toEqual(commands);
  expect(entries).toHaveLength(3);
});

it("does not settle commands from unrelated turn completion, process loss, or model observations", () => {
  const entries = [
    admission(1n, MODEL),
    admission(2n, INTERRUPT),
    admission(3n, MESSAGE),
    entry(4n, { case: "modelChanged", value: { model: "next" } }),
    entry(5n, { case: "turnCompleted", value: { turnId: "turn", status: TurnStatus.COMPLETED } }),
    entry(6n, { case: "harnessLost", value: {} }),
  ];
  expect(observedCommands(entries).every((command) => command.outcome === null)).toBe(true);
});

it.each(["commandFailed", "commandNoop"] as const)("retains %s as terminal non-effect evidence", (kind) => {
  const outcome = entry(2n, { case: kind, value: { commandId: MODEL.commandId, reason: "cannot apply" } });
  const commands = observedCommands([admission(1n, MODEL), outcome]);
  expect(commands[0].outcome).toBe(outcome);
  expect(commands[0].outcome?.event?.observation.case).toBe(kind);
});
