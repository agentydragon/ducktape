import { describe, expect, it } from "vitest";

import { renderPreview } from "./entry.tsx";
import { hakuRoutinePreviews } from "./haku_routine.tsx";

describe("hakuRoutinePreviews", () => {
  it("renders launch_routine with per-run instructions, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderPreview(hakuRoutinePreviews.launch_routine, { text: "triage open PRs" }, variant)).not.toBeNull();
    }
  });

  it("renders launch_routine with no text (routine default)", () => {
    expect(renderPreview(hakuRoutinePreviews.launch_routine, {}, "detailed")).not.toBeNull();
  });
});
