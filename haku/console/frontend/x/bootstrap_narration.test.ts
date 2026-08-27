import { describe, expect, it } from "vitest";

import type { ConversationSession } from "../client";
import { bootstrapNarration, type NarrationLine } from "./bootstrap_narration";

function line(frameSeq: number, text: string): NarrationLine {
  return { kind: "setup_output", frame_seq: frameSeq, text, created_at: "2026-08-01T03:00:00Z" };
}

function session(
  overrides: Partial<Pick<ConversationSession, "status" | "narration">>
): Pick<ConversationSession, "status" | "narration"> {
  return { status: "ready", narration: [], ...overrides };
}

describe("bootstrapNarration", () => {
  it("shows nothing for a session that narrated nothing", () => {
    expect(bootstrapNarration(session({}), false)).toBeNull();
  });

  it("orders lines by frame_seq rather than by the order they arrived in", () => {
    const narration = bootstrapNarration(session({ narration: [line(9, "done."), line(4, "Cloning…")] }), true);
    expect(narration?.lines.map((entry) => entry.text)).toEqual(["Cloning…", "done."]);
  });

  it("keeps two identical lines, because a repeat is not a replay", () => {
    const narration = bootstrapNarration(session({ narration: [line(4, "retrying"), line(5, "retrying")] }), true);
    expect(narration?.lines.map((entry) => entry.frame_seq)).toEqual([4, 5]);
  });

  it("opens expanded while the session is still provisioning, even once the transcript has rows", () => {
    const narration = bootstrapNarration(session({ status: "provisioning", narration: [line(1, "Cloning…")] }), false);
    expect(narration?.startsExpanded).toBe(true);
  });

  it("opens expanded for a session that failed before recording a transcript", () => {
    const narration = bootstrapNarration(
      session({ status: "failed", narration: [line(1, "fatal: repo not found")] }),
      true
    );
    expect(narration?.startsExpanded).toBe(true);
  });

  it("collapses once the transcript is what the operator came for", () => {
    const narration = bootstrapNarration(session({ narration: [line(1, "Cloning…")] }), false);
    expect(narration?.startsExpanded).toBe(false);
  });
});
