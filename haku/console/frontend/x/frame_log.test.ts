import { describe, expect, it } from "vitest";

import type { SessionFrame, SessionFramePage } from "../client";
import { frameSummary, kindsForMode, prependEarlierPage } from "./frame_log";

function frame(frame_seq: number, kind: string, payload: Record<string, unknown>): SessionFrame {
  return {
    frame_seq,
    kind,
    payload,
    direction: "from_agent",
    partial: false,
    created_at: "2026-08-01T03:00:00Z",
  };
}

function page(frames: SessionFrame[], next_before_seq: number | null): SessionFramePage {
  return { frames, next_before_seq };
}

describe("kindsForMode", () => {
  it("names no kind for the default view, so an unknown frame type still arrives", () => {
    expect(kindsForMode("frames")).toBeUndefined();
  });

  it("names the delta kind alone, since that is what asking for the stream means", () => {
    expect(kindsForMode("deltas")).toEqual(["stream_event"]);
  });
});

describe("prependEarlierPage", () => {
  it("places an earlier page above what is loaded and carries its cursor", () => {
    const loaded = page([frame(10, "result", {})], 10);

    const merged = prependEarlierPage(page([frame(8, "user", {}), frame(9, "assistant", {})], 8), loaded);

    expect(merged.frames.map((f) => f.frame_seq)).toEqual([8, 9, 10]);
    expect(merged.next_before_seq).toBe(8);
  });

  it("shows a frame once when the same page arrives twice", () => {
    const earlier = page([frame(8, "user", {})], 8);
    const loaded = prependEarlierPage(earlier, page([frame(10, "result", {})], 10));

    expect(prependEarlierPage(earlier, loaded).frames.map((f) => f.frame_seq)).toEqual([8, 10]);
  });

  it("keeps the last cursor, so exhausting the log stops the walk", () => {
    const merged = prependEarlierPage(page([frame(1, "system", {})], null), page([frame(2, "user", {})], 2));

    expect(merged.next_before_seq).toBeNull();
  });
});

describe("frameSummary", () => {
  it("reads an assistant answer's leading text", () => {
    const payload = { message: { content: [{ type: "text", text: "Looking at\n  the logs now." }] } };

    expect(frameSummary(frame(1, "assistant", payload))).toBe("Looking at the logs now.");
  });

  it("names the tools a batched assistant frame called, and counts the rest", () => {
    const payload = {
      message: {
        content: [
          { type: "tool_use", name: "Bash", input: {} },
          { type: "tool_use", name: "Read", input: {} },
          { type: "tool_use", name: "Grep", input: {} },
        ],
      },
    };

    expect(frameSummary(frame(2, "assistant", payload))).toBe("Bash() · Read() · +1 more");
  });

  it("points a tool result at the call it answers", () => {
    const payload = { message: { content: [{ type: "tool_result", tool_use_id: "toolu_01" }] } };

    expect(frameSummary(frame(3, "user", payload))).toBe("result → toolu_01");
  });

  it("reads a prompt whose content is a bare string", () => {
    expect(frameSummary(frame(4, "user", { message: { content: "What is happening?" } }))).toBe("What is happening?");
  });

  it("says how a result frame ended", () => {
    expect(frameSummary(frame(5, "result", { subtype: "success" }))).toBe("success");
    expect(frameSummary(frame(6, "result", { subtype: "error_during_execution", is_error: true }))).toBe(
      "error_during_execution · error"
    );
  });

  it("reads the text out of a delta and the line out of setup output", () => {
    expect(frameSummary(frame(7, "stream_event", { event: { delta: { text: "part" } } }))).toBe("part");
    expect(frameSummary(frame(8, "setup_output", { text: "cloning haku-state" }))).toBe("cloning haku-state");
  });

  it("summarises a shape it does not recognise to nothing rather than guessing", () => {
    expect(frameSummary(frame(9, "invented_by_a_later_cli", { whatever: [1, 2] }))).toBe("");
  });

  it("cuts a long line rather than wrapping the row", () => {
    const payload = { message: { content: [{ type: "text", text: "x".repeat(400) }] } };

    expect(frameSummary(frame(10, "assistant", payload))).toHaveLength(141);
  });
});
