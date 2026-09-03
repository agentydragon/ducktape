import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";

import { EMPTY, reduce } from "./events";
import { Direction, EventSchema, ItemKind, TurnStatus, type Event } from "./protocol_pb";

function event(sequence: number, observation: MessageInitShape<typeof EventSchema>["observation"]): Event {
  return create(EventSchema, { sequence: BigInt(sequence), observation });
}

const script: Event[] = [
  event(1, { case: "harnessStarted", value: { resumed: false, pid: 7 } }),
  event(2, { case: "turnStarted", value: { turnId: "t1" } }),
  event(3, { case: "inputSubmitted", value: { inputId: "i1", text: "hi" } }),
  event(4, { case: "inputAccepted", value: { inputId: "i1", turnId: "t1" } }),
  event(5, { case: "itemStarted", value: { itemId: "m#0", kind: ItemKind.ASSISTANT_TEXT } }),
  event(6, { case: "textDelta", value: { itemId: "m#0", text: "Hel" } }),
  event(7, { case: "textDelta", value: { itemId: "m#0", text: "lo" } }),
  event(8, { case: "itemStarted", value: { itemId: "toolu_1", kind: ItemKind.TOOL_CALL, toolName: "Bash" } }),
  event(9, { case: "toolArgumentsDelta", value: { itemId: "toolu_1", partialJson: '{"command":' } }),
  event(10, { case: "toolArguments", value: { itemId: "toolu_1", argumentsJson: '{"command": "ls"}' } }),
  event(11, { case: "native", value: { direction: Direction.FROM_HARNESS, line: "{}" } }),
  event(12, {
    case: "itemCompleted",
    value: { itemId: "toolu_1", outcome: { case: "tool", value: { output: "a\nb", succeeded: true } } },
  }),
  event(13, { case: "itemCompleted", value: { itemId: "m#0", outcome: { case: "text", value: "Hello" } } }),
  event(14, { case: "turnCompleted", value: { turnId: "t1", status: TurnStatus.COMPLETED } }),
];

describe("reduce", () => {
  it("folds a turn's events into items in order, with streamed text and tool results", () => {
    const state = script.reduce(reduce, EMPTY);
    expect(state.harness).toBe("running");
    expect(state.lastSequence).toBe("14");
    expect(state.turns).toEqual([{ id: "t1", status: TurnStatus.COMPLETED, error: "", itemIds: ["m#0", "toolu_1"] }]);
    expect(state.items["m#0"]).toMatchObject({ kind: ItemKind.ASSISTANT_TEXT, text: "Hello", completed: true });
    expect(state.items["toolu_1"]).toMatchObject({
      toolName: "Bash",
      argumentsJson: '{"command": "ls"}',
      output: "a\nb",
      succeeded: true,
      completed: true,
    });
    expect(state.inputs).toEqual([{ id: "i1", state: "accepted", detail: "", text: "hi", turnId: "t1" }]);
  });

  it("keeps an input's rejection reason and a lost harness", () => {
    const state = [
      event(1, { case: "inputSubmitted", value: { inputId: "i1" } }),
      event(2, { case: "inputRejected", value: { inputId: "i1", reason: "nope" } }),
      event(3, { case: "harnessLost", value: {} }),
    ].reduce(reduce, EMPTY);
    expect(state.inputs).toEqual([{ id: "i1", state: "rejected", detail: "nope", text: "", turnId: null }]);
    expect(state.harness).toBe("lost");
  });
});
