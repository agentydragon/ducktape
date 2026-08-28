import { describe, expect, it } from "vitest";

import type { Session } from "../client";
import { bootstrapNarration, type NarrationLine } from "./bootstrap_narration";

function line(frameSeq: number, text: string): NarrationLine {
  return { kind: "setup_output", frame_seq: frameSeq, text, created_at: "2026-08-01T03:00:00Z" };
}

function conversation(overrides: { status?: Session["status"]; narration?: NarrationLine[] }): {
  narration: NarrationLine[];
  session: Pick<Session, "status">;
} {
  return { narration: overrides.narration ?? [], session: { status: overrides.status ?? "ready" } };
}

describe("bootstrapNarration", () => {
  it("shows nothing for a session that narrated nothing", () => {
    expect(bootstrapNarration(conversation({}), false)).toBeNull();
  });

  it("orders lines by frame_seq rather than by the order they arrived in", () => {
    const narration = bootstrapNarration(conversation({ narration: [line(9, "done."), line(4, "Cloning…")] }), true);
    expect(narration?.lines.map((entry) => entry.text)).toEqual(["Cloning…", "done."]);
  });

  it("keeps two identical lines, because a repeat is not a replay", () => {
    const narration = bootstrapNarration(conversation({ narration: [line(4, "retrying"), line(5, "retrying")] }), true);
    expect(narration?.lines.map((entry) => entry.frame_seq)).toEqual([4, 5]);
  });

  it("opens expanded while the session is still provisioning, even once the transcript has rows", () => {
    const narration = bootstrapNarration(
      conversation({ status: "provisioning", narration: [line(1, "Cloning…")] }),
      false
    );
    expect(narration?.startsExpanded).toBe(true);
  });

  it("opens expanded for a session that failed before recording a transcript", () => {
    const narration = bootstrapNarration(
      conversation({ status: "failed", narration: [line(1, "fatal: repo not found")] }),
      true
    );
    expect(narration?.startsExpanded).toBe(true);
  });

  it("collapses once the transcript is what the operator came for", () => {
    const narration = bootstrapNarration(conversation({ narration: [line(1, "Cloning…")] }), false);
    expect(narration?.startsExpanded).toBe(false);
  });
});
