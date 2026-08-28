import { describe, expect, it } from "vitest";

import type { SessionFrame, SessionFramePage } from "../client";
import { prependEarlierPage } from "./frame_log";

function frame(frame_seq: number, payload: Record<string, unknown> = {}): SessionFrame {
  return {
    frame_seq,
    kind: "harness_frame",
    payload,
    direction: "from_agent",
    created_at: "2026-08-01T03:00:00Z",
  };
}

function page(frames: SessionFrame[], next_before_seq: number | null): SessionFramePage {
  return {
    frames,
    conversation_id: "70000000-0000-4000-8000-000000000001",
    runtime_kind: "claude_code",
    harness_kind: "claude_code",
    next_before_seq,
  };
}

describe("prependEarlierPage", () => {
  it("places an earlier page above what is loaded and carries its cursor", () => {
    const loaded = page([frame(10, { 阶段: "最终" })], 10);

    const merged = prependEarlierPage(page([frame(8, { 动作: "输入" }), frame(9, { 阶段: "碎片" })], 8), loaded);

    expect(merged.frames.map((f) => f.frame_seq)).toEqual([8, 9, 10]);
    expect(merged.next_before_seq).toBe(8);
  });

  it("shows a frame once when the same page arrives twice", () => {
    const earlier = page([frame(8)], 8);
    const loaded = prependEarlierPage(earlier, page([frame(10)], 10));

    expect(prependEarlierPage(earlier, loaded).frames.map((f) => f.frame_seq)).toEqual([8, 10]);
  });

  it("keeps the last cursor, so exhausting the log stops the walk", () => {
    const merged = prependEarlierPage(page([frame(1)], null), page([frame(2)], 2));

    expect(merged.next_before_seq).toBeNull();
  });
});
