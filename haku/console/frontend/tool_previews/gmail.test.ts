import { describe, expect, it } from "vitest";

import { gmailToolPreview } from "./gmail.tsx";

describe("gmailToolPreview", () => {
  it("renders threads_batch_modify for valid args, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const preview = gmailToolPreview(
        "threads_batch_modify",
        { thread_ids: ["t1"], add: ["urgent"], remove: [] },
        variant
      );
      expect(preview).not.toBeNull();
      expect(preview).not.toBe(false);
    }
  });

  it("renders drafts_create for valid args, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        gmailToolPreview("drafts_create", { to: ["a@example.com"], subject: "Hello", body: "Hi there" }, variant)
      ).not.toBeNull();
    }
  });

  it("returns null when threads_batch_modify args are malformed", () => {
    // thread_ids is min_length=1; an empty list fails the schema, so the widget returns null
    // and the caller shows raw JSON rather than a blank Arguments field.
    expect(gmailToolPreview("threads_batch_modify", { thread_ids: [], add: ["x"] }, "compact")).toBeNull();
  });

  it("returns null for read tools (self-descriptive args → raw JSON, no custom widget)", () => {
    expect(gmailToolPreview("threads_list", { query: "from:a" }, "detailed")).toBeNull();
  });
});
