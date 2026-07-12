import { describe, expect, it } from "vitest";

import { renderResultPreview } from "./entry.tsx";
import { gmailResultPreviews } from "./gmail.tsx";

describe("gmailResultPreviews", () => {
  it("renders drafts_create for a Draft resource, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderResultPreview(
          gmailResultPreviews.drafts_create,
          { id: "r-7364618394", message: { id: "18c2f0a", threadId: "t42" } },
          variant
        )
      ).not.toBeNull();
    }
  });

  it("returns null when the draft id is missing (→ raw JSON fallback)", () => {
    expect(renderResultPreview(gmailResultPreviews.drafts_create, { message: { id: "m1" } }, "detailed")).toBeNull();
  });
});
