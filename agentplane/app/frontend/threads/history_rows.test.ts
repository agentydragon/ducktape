import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";

import { EventSchema, ItemKind, TurnStatus } from "../../../protocol/event_pb";
import { historyRows, rowKey, summarizeLifecycleGroup, summarizeRun, summarizeSetup } from "./history_rows";
import { testEntity, testItem } from "./thread_entity_fixture";
import type { ThreadEntity } from "./thread_sync";

/** A lifecycle entity carrying a real, parseable `Event`: grouping a lifecycle observation depends
 * on what it says, not just its kind. */
function lifecycleItem(
  cursor: number,
  observation: string,
  value: MessageInitShape<typeof EventSchema>["observation"]
): ThreadEntity {
  return testEntity(cursor, "lifecycle", {
    observation,
    event: toJson(EventSchema, create(EventSchema, { observation: value })),
  });
}

const TURN_STARTED = (turnId: string) => lifecycleItem(1, "turn_started", { case: "turnStarted", value: { turnId } });
const HARNESS_STARTED = (cursor: number) =>
  lifecycleItem(cursor, "harness_started", { case: "harnessStarted", value: {} });
const TURN_COMPLETED = (cursor: number, status: TurnStatus = TurnStatus.COMPLETED) =>
  lifecycleItem(cursor, "turn_completed", { case: "turnCompleted", value: { status } });
const HARNESS_LOST = (cursor: number) => lifecycleItem(cursor, "harness_lost", { case: "harnessLost", value: {} });

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

  it("ends a run at confirmed input and at a mundane lifecycle observation", () => {
    const segments = [
      testItem(1, ItemKind.TOOL_CALL),
      TURN_COMPLETED(2),
      testItem(3, ItemKind.TOOL_CALL),
      testEntity(4, "confirmed_input", { harness_message_id: "test-input", origin_command_ids: [] }),
      testItem(5, ItemKind.TOOL_CALL),
    ];
    expect(historyRows(segments).map((row) => row.kind)).toEqual(["run", "lifecycle_group", "run", "entity", "run"]);
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

  it("groups consecutive mundane lifecycle observations", () => {
    const segments = [TURN_STARTED("t1"), HARNESS_STARTED(2), TURN_COMPLETED(3)];
    expect(historyRows(segments)).toEqual([{ kind: "lifecycle_group", entities: segments }]);
  });

  it("keeps a prominent lifecycle observation standing alone, breaking a group around it", () => {
    const segments = [TURN_STARTED("t1"), HARNESS_LOST(2), HARNESS_STARTED(3)];
    expect(historyRows(segments).map((row) => ({ kind: row.kind, count: row.entities.length }))).toEqual([
      { kind: "lifecycle_group", count: 1 },
      { kind: "entity", count: 1 },
      { kind: "lifecycle_group", count: 1 },
    ]);
  });

  it("does not group a turn's failed completion, which carries its own diagnostic", () => {
    const segments = [HARNESS_STARTED(1), TURN_COMPLETED(2, TurnStatus.FAILED)];
    expect(historyRows(segments).map((row) => row.kind)).toEqual(["lifecycle_group", "entity"]);
  });

  it("folds setup output into one readable row while retaining every event", () => {
    const segments = [
      lifecycleItem(1, "setup_started", { case: "setupStarted", value: {} }),
      lifecycleItem(2, "setup_output", {
        case: "setupOutput",
        value: { stream: { case: "stdout", value: new Uint8Array([111, 107]) } },
      }),
      lifecycleItem(3, "setup_finished", { case: "setupFinished", value: { exitCode: 7 } }),
      HARNESS_STARTED(4),
    ];
    expect(historyRows(segments).map((row) => row.kind)).toEqual(["setup", "lifecycle_group"]);
    expect(historyRows(segments)[0]?.entities).toEqual(segments.slice(0, 3));
    expect(summarizeSetup(segments.slice(0, 2))).toBe("Thread setup running");
    expect(summarizeSetup(segments.slice(0, 3))).toBe("Thread setup failed (exit 7)");
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

describe("summarizeLifecycleGroup", () => {
  it("comma-joins each entity's own label in order", () => {
    const segments = [TURN_STARTED("t1"), HARNESS_STARTED(2), TURN_COMPLETED(3)];
    expect(summarizeLifecycleGroup(segments)).toBe("Turn started, Harness started, Turn completed");
  });
});
