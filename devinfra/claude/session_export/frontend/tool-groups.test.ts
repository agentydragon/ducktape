import { expect, it } from "vitest";

import { noisySessionEvents } from "./fixtures/noisy-session";
import { foldSessionEvents, type TranscriptItem, type TranscriptToolRun } from "./transcript";
import { groupToolActivity, toolGroupSummary } from "./tool-groups";

const transcript = foldSessionEvents(noisySessionEvents);
const runs = transcript.filter((item): item is TranscriptToolRun => item.kind === "tool-run");
const first = runs[0]!;
const second = runs[1]!;
const thinking = transcript.find((item) => item.kind === "thinking")!;

it("groups adjacent work across event boundaries and retains every original item", () => {
  const grouped = groupToolActivity(transcript);
  expect(grouped.length).toBeLessThan(transcript.length);
  expect(grouped.flatMap((row) => (row.kind === "tool-group" ? row.items : [row]))).toEqual(transcript);
  const group = grouped.find((row) => row.kind === "tool-group")!;
  if (group.kind !== "tool-group") throw new Error("Expected an activity group");
  expect(toolGroupSummary(group)).toBe("Read × 2 · Bash × 2 · Grep × 2");
  expect(group.items.some((row) => row.kind === "thinking")).toBe(true);
  expect(new Set(group.events.map((event) => event.event_id)).size).toBe(group.events.length);
});

it.each<TranscriptItem>([
  { kind: "message", id: "user", events: [], role: "user", text: "Wait" },
  { kind: "message", id: "assistant", events: [], role: "assistant", text: "Here is what I found" },
  { kind: "notice", id: "warning", events: [], title: "Permission required" },
  { kind: "summary", id: "end", events: [], title: "Turn complete", details: [] },
  { kind: "peer-hold", id: "peer", events: [], messageUuid: "peer", from: "review", state: "held" },
])("keeps $kind boundaries visible", (boundary) => {
  expect(groupToolActivity([first, boundary, second])).toEqual([first, boundary, second]);
});

it("keeps standalone tools and subagent scopes separate", () => {
  const standalone = { ...first, standalone: true };
  const child = { ...second, parentToolUseId: "parent-tool" };
  expect(groupToolActivity([first, child, second])).toEqual([first, child, second]);
  expect(groupToolActivity([standalone, second])).toEqual([standalone, second]);
});

it("retains individual disclosures when there is only one run", () => {
  expect(groupToolActivity([thinking, first, thinking])).toEqual([thinking, first, thinking]);
  expect(groupToolActivity([])).toEqual([]);
});

it("keeps failed and unfinished tool states available to the group header", () => {
  const failed: TranscriptToolRun = {
    ...first,
    tools: first.tools.map((tool) => ({ ...tool, status: "error" })),
    status: "error",
  };
  const running: TranscriptToolRun = {
    ...second,
    tools: second.tools.map((tool) => ({ ...tool, status: "running" })),
    status: "running",
  };
  const group = groupToolActivity([failed, running])[0]!;
  if (group.kind !== "tool-group") throw new Error("Expected an activity group");
  expect(group.items).toEqual([failed, running]);
});
