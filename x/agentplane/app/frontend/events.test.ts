import { describe, expect, it } from "vitest";

import type { Event } from "./client";
import { EMPTY, reduce } from "./events";

const script: Event[] = [
  { sequence: "1", harnessStarted: { resumed: false, pid: 7 } },
  { sequence: "2", turnStarted: { turnId: "t1" } },
  { sequence: "3", inputSubmitted: { inputId: "i1" } },
  { sequence: "4", inputAccepted: { inputId: "i1", turnId: "t1" } },
  { sequence: "5", itemStarted: { itemId: "m#0", kind: "ITEM_KIND_ASSISTANT_TEXT" } },
  { sequence: "6", textDelta: { itemId: "m#0", text: "Hel" } },
  { sequence: "7", textDelta: { itemId: "m#0", text: "lo" } },
  { sequence: "8", itemStarted: { itemId: "toolu_1", kind: "ITEM_KIND_TOOL_CALL", toolName: "Bash" } },
  { sequence: "9", toolArgumentsDelta: { itemId: "toolu_1", partialJson: '{"command":' } },
  { sequence: "10", toolArguments: { itemId: "toolu_1", argumentsJson: '{"command": "ls"}' } },
  { sequence: "11", native: { direction: "DIRECTION_FROM_HARNESS", line: "{}" } },
  { sequence: "12", itemCompleted: { itemId: "toolu_1", tool: { output: "a\nb", succeeded: true } } },
  { sequence: "13", itemCompleted: { itemId: "m#0", text: "Hello" } },
  { sequence: "14", turnCompleted: { turnId: "t1", status: "TURN_STATUS_COMPLETED" } },
];

describe("reduce", () => {
  it("folds a turn's events into items in order, with streamed text and tool results", () => {
    const state = script.reduce(reduce, EMPTY);
    expect(state.harness).toBe("running");
    expect(state.lastSequence).toBe("14");
    expect(state.turns).toEqual([
      { id: "t1", status: "TURN_STATUS_COMPLETED", error: "", itemIds: ["m#0", "toolu_1"] },
    ]);
    expect(state.items["m#0"]).toMatchObject({ kind: "ITEM_KIND_ASSISTANT_TEXT", text: "Hello", completed: true });
    expect(state.items["toolu_1"]).toMatchObject({
      toolName: "Bash",
      argumentsJson: '{"command": "ls"}',
      output: "a\nb",
      succeeded: true,
      completed: true,
    });
    expect(state.inputs).toEqual([{ id: "i1", state: "accepted", detail: "" }]);
  });

  it("keeps an input's rejection reason and a lost harness", () => {
    const state = [
      { sequence: "1", inputSubmitted: { inputId: "i1" } },
      { sequence: "2", inputRejected: { inputId: "i1", reason: "nope" } },
      { sequence: "3", harnessLost: {} },
    ].reduce(reduce, EMPTY);
    expect(state.inputs).toEqual([{ id: "i1", state: "rejected", detail: "nope" }]);
    expect(state.harness).toBe("lost");
  });
});
