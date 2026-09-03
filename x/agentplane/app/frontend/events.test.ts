import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";

import { EMPTY, reduce, timeline, type Row } from "./events";
import { Direction, EventSchema, ItemKind, TurnStatus, type Event } from "./protocol_pb";

function event(
  sequence: number,
  observation: MessageInitShape<typeof EventSchema>["observation"],
  sources: number[] = []
): Event {
  return create(EventSchema, { sequence: BigInt(sequence), observation, sourceSequences: sources.map(BigInt) });
}

function rowId(row: Row): string {
  switch (row.kind) {
    case "turn":
      return row.turn.id;
    case "input":
      return row.input.id;
    case "item":
      return row.item.id;
  }
}

const script: Event[] = [
  event(1, { case: "harnessStarted", value: { resumed: false, pid: 7 } }),
  event(2, { case: "turnStarted", value: { turnId: "t1" } }),
  event(3, { case: "inputSubmitted", value: { inputId: "i1", text: "hi" } }),
  event(4, { case: "native", value: { direction: Direction.TO_HARNESS, line: '{"text":"hi"}' } }),
  event(5, { case: "inputAccepted", value: { inputId: "i1", turnId: "t1" } }, [4]),
  event(6, { case: "native", value: { direction: Direction.FROM_HARNESS, line: '{"delta":"Hel"}' } }),
  event(7, { case: "itemStarted", value: { itemId: "m#0", kind: ItemKind.ASSISTANT_TEXT } }, [6]),
  event(8, { case: "textDelta", value: { itemId: "m#0", text: "Hel" } }, [6]),
  event(9, { case: "native", value: { direction: Direction.FROM_HARNESS, line: '{"delta":"lo"}' } }),
  event(10, { case: "textDelta", value: { itemId: "m#0", text: "lo" } }, [9]),
  event(11, { case: "itemStarted", value: { itemId: "toolu_1", kind: ItemKind.TOOL_CALL, toolName: "Bash" } }),
  event(12, { case: "toolArgumentsDelta", value: { itemId: "toolu_1", partialJson: '{"command":' } }),
  event(13, { case: "toolArguments", value: { itemId: "toolu_1", argumentsJson: '{"command": "ls"}' } }),
  event(14, { case: "native", value: { direction: Direction.FROM_HARNESS, line: "{}" } }),
  event(15, {
    case: "itemCompleted",
    value: { itemId: "toolu_1", outcome: { case: "tool", value: { output: "a\nb", succeeded: true } } },
  }),
  event(16, { case: "itemCompleted", value: { itemId: "m#0", outcome: { case: "text", value: "Hello" } } }),
  // Between two items: the position is the whole point of showing it.
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
});

describe("timeline", () => {
  it("is every event once, in ascending sequence order", () => {
    const inOrder = script.map((event) => String(event.sequence));
    expect(timeline(script.reduce(reduce, EMPTY)).map((step) => String(step.event.sequence))).toEqual(inOrder);
    // Backwards through the fold, so the ordering is the comparison's doing and not arrival's — and
    // a decimal-string comparison would put 10 before 2.
    expect(timeline([...script].reverse().reduce(reduce, EMPTY)).map((step) => String(step.event.sequence))).toEqual(
      inOrder
    );
  });

  it("announces each row at the sequence that started it, and nowhere else", () => {
    const steps = timeline(script.reduce(reduce, EMPTY));
    // The row renders above its own event, so one sequence carries both.
    const announced = steps.flatMap((step) =>
      step.row ? [[String(step.event.sequence), step.row.kind, rowId(step.row)]] : []
    );
    expect(announced).toEqual([
      ["2", "turn", "t1"],
      ["3", "input", "i1"],
      ["7", "item", "m#0"],
      ["11", "item", "toolu_1"],
    ]);
  });

  it("leaves what no row was built from where it happened", () => {
    const steps = timeline(script.reduce(reduce, EMPTY));
    const stderr = steps.findIndex((step) => step.event.observation.case === "harnessStderr");
    const completed = steps.findIndex((step) => step.event.observation.case === "turnCompleted");
    const lastItem = steps.findIndex((step) => step.event.sequence === 16n);
    expect(lastItem).toBeLessThan(stderr);
    expect(stderr).toBeLessThan(completed);
  });
});
