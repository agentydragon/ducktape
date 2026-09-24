import { describe, expect, it } from "vitest";

import { ItemKind } from "../../protocol/event_pb";
import { historyRows, rowKey, summarizeRun } from "./history_rows";
import { testEntity, testItem } from "./thread_entity_fixture";

describe("historyRows", () => {
  it("runs consecutive tool calls and reasoning together, leaving other entities standing alone", () => {
    const segments = [
      testItem(1, ItemKind.TOOL_CALL),
      testItem(2, ItemKind.REASONING),
      testItem(3, ItemKind.TOOL_CALL),
      testItem(4, ItemKind.ASSISTANT_TEXT),
      testItem(5, ItemKind.REASONING),
    ];
    expect(historyRows(segments)).toEqual([
      { kind: "run", entities: segments.slice(0, 3) },
      { kind: "entity", entities: [segments[3]] },
      { kind: "run", entities: [segments[4]] },
    ]);
  });

  it("ends a run at confirmed input and at lifecycle observations", () => {
    const segments = [
      testItem(1, ItemKind.TOOL_CALL),
      testEntity(2, "lifecycle", { observation: "turn_completed", event: null }),
      testItem(3, ItemKind.TOOL_CALL),
      testEntity(4, "confirmed_input", { harness_message_id: "test-input", origin_command_ids: [] }),
      testItem(5, ItemKind.TOOL_CALL),
    ];
    expect(historyRows(segments).map((row) => row.kind)).toEqual(["run", "entity", "run", "entity", "run"]);
  });

  it("keeps a run's key while later steps stream into it", () => {
    const segments = [
      testItem(1, ItemKind.ASSISTANT_TEXT),
      testItem(2, ItemKind.REASONING),
      testItem(3, ItemKind.TOOL_CALL),
    ];
    const keys = (count: number) => historyRows(segments.slice(0, count)).map(rowKey);
    expect(keys(3)).toEqual(keys(2));
  });
});

describe("summarizeRun", () => {
  it("counts tool calls and reasoning steps separately", () => {
    const tool = testItem(1, ItemKind.TOOL_CALL);
    const reasoning = testItem(2, ItemKind.REASONING);
    expect(summarizeRun([tool, tool, reasoning])).toBe("2 tool calls, 1 reasoning step");
    expect(summarizeRun([tool])).toBe("1 tool call");
    expect(summarizeRun([reasoning, reasoning])).toBe("2 reasoning steps");
  });
});
