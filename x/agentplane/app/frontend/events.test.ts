import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";

import { EMPTY, reduce, type Frames } from "./events";
import { Direction, EventSchema, ItemKind, TurnStatus, type Event } from "./protocol_pb";

function event(
  sequence: number,
  observation: MessageInitShape<typeof EventSchema>["observation"],
  sources: number[] = []
): Event {
  return create(EventSchema, { sequence: BigInt(sequence), observation, sourceSequences: sources.map(BigInt) });
}

function sequences(frames: Frames): string[] {
  return frames.map((frame) => String(frame.sequence));
}

const script: Event[] = [
  event(1, { case: "harnessStarted", value: { resumed: false, pid: 7 } }),
  event(2, { case: "turnStarted", value: { turnId: "t1" } }),
  event(3, { case: "inputSubmitted", value: { inputId: "i1", text: "hi" } }),
  event(4, { case: "native", value: { direction: Direction.TO_HARNESS, line: '{"text":"hi"}' } }),
  event(5, { case: "inputAccepted", value: { inputId: "i1", turnId: "t1" } }, [4]),
  event(6, { case: "native", value: { direction: Direction.FROM_HARNESS, line: '{"delta":"Hel"}' } }),
  // One frame translating into two events: the frame belongs to the item either way.
  event(7, { case: "itemStarted", value: { itemId: "m#0", kind: ItemKind.ASSISTANT_TEXT } }, [6]),
  event(8, { case: "textDelta", value: { itemId: "m#0", text: "Hel" } }, [6]),
  event(9, { case: "native", value: { direction: Direction.FROM_HARNESS, line: '{"delta":"lo"}' } }),
  event(10, { case: "textDelta", value: { itemId: "m#0", text: "lo" } }, [9]),
  event(11, { case: "itemStarted", value: { itemId: "toolu_1", kind: ItemKind.TOOL_CALL, toolName: "Bash" } }),
  event(12, { case: "toolArgumentsDelta", value: { itemId: "toolu_1", partialJson: '{"command":' } }),
  event(13, { case: "toolArguments", value: { itemId: "toolu_1", argumentsJson: '{"command": "ls"}' } }),
  // A frame nothing derives from, so nothing but the raw view accounts for it.
  event(14, { case: "native", value: { direction: Direction.FROM_HARNESS, line: "{}" } }),
  event(15, {
    case: "itemCompleted",
    value: { itemId: "toolu_1", outcome: { case: "tool", value: { output: "a\nb", succeeded: true } } },
  }),
  event(16, { case: "itemCompleted", value: { itemId: "m#0", outcome: { case: "text", value: "Hello" } } }),
  event(17, { case: "harnessStderr", value: { text: "a warning\n" } }),
  event(18, { case: "turnCompleted", value: { turnId: "t1", status: TurnStatus.COMPLETED } }),
];

describe("reduce", () => {
  it("folds a turn's events into items in order, with streamed text and tool results", () => {
    const state = script.reduce(reduce, EMPTY);
    expect(state.harness).toBe("running");
    expect(state.lastSequence).toBe("18");
    expect(state.turns).toMatchObject([
      { id: "t1", status: TurnStatus.COMPLETED, error: "", itemIds: ["m#0", "toolu_1"] },
    ]);
    expect(state.items["m#0"]).toMatchObject({ kind: ItemKind.ASSISTANT_TEXT, text: "Hello", completed: true });
    expect(state.items["toolu_1"]).toMatchObject({
      toolName: "Bash",
      argumentsJson: '{"command": "ls"}',
      output: "a\nb",
      succeeded: true,
      completed: true,
    });
    expect(state.inputs).toMatchObject([{ id: "i1", state: "accepted", detail: "", text: "hi", turnId: "t1" }]);
  });

  it("keeps an input's rejection reason and a lost harness", () => {
    const state = [
      event(1, { case: "inputSubmitted", value: { inputId: "i1" } }),
      event(2, { case: "inputRejected", value: { inputId: "i1", reason: "nope" } }),
      event(3, { case: "harnessLost", value: {} }),
    ].reduce(reduce, EMPTY);
    expect(state.inputs).toMatchObject([{ id: "i1", state: "rejected", detail: "nope", text: "", turnId: null }]);
    expect(state.harness).toBe("lost");
  });

  it("keeps each row's own events, with the native frames they name", () => {
    const state = script.reduce(reduce, EMPTY);
    expect(sequences(state.items["m#0"].frames)).toEqual(["6", "7", "8", "9", "10", "16"]);
    expect(sequences(state.items["toolu_1"].frames)).toEqual(["11", "12", "13", "15"]);
    expect(sequences(state.inputs[0].frames)).toEqual(["3", "4", "5"]);
    expect(sequences(state.turns[0].frames)).toEqual(["2", "18"]);
  });

  it("leaves what no row produced loose: harness lifecycle, stderr, an untranslated frame", () => {
    const state = script.reduce(reduce, EMPTY);
    expect(sequences(state.looseFrames)).toEqual(["1", "14", "17"]);
  });

  it("attributes every event to exactly one row, so the raw view hides nothing", () => {
    const state = script.reduce(reduce, EMPTY);
    const shown = sequences([
      ...state.looseFrames,
      ...state.turns.flatMap((turn) => turn.frames),
      ...state.inputs.flatMap((input) => input.frames),
      ...Object.values(state.items).flatMap((item) => item.frames),
    ]);
    expect(new Set(shown)).toEqual(new Set(sequences(script)));
    expect(shown.length).toBe(script.length);
  });
});
