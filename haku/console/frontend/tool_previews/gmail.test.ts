import { describe, expect, it } from "vitest";

import { describeAction, renderPreview } from "./entry.tsx";
import { gmailPreviews } from "./gmail.tsx";

describe("gmailPreviews", () => {
  it("renders threads_modify_labels for valid args, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      const node = renderPreview(
        gmailPreviews.threads_modify_labels,
        { thread_ids: ["t1"], add: ["urgent"], remove: [] },
        variant
      );
      expect(node).not.toBeNull();
    }
  });

  it("renders drafts_create for valid args, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderPreview(
          gmailPreviews.drafts_create,
          { to: ["a@example.com"], subject: "Hello", body: "Hi there" },
          variant
        )
      ).not.toBeNull();
    }
  });

  it("keeps custom previews and action text when nullable FastMCP arguments are explicitly null", () => {
    const relabelArgs = { thread_ids: ["t1"], add: ["Follow up"], remove: null };
    const draftArgs = {
      to: ["a@example.com"],
      subject: "Hello",
      body: "Hi there",
      cc: null,
      thread_id: null,
    };

    expect(renderPreview(gmailPreviews.threads_modify_labels, relabelArgs, "compact")).not.toBeNull();
    expect(renderPreview(gmailPreviews.drafts_create, draftArgs, "compact")).not.toBeNull();
    expect(describeAction(gmailPreviews.threads_modify_labels, relabelArgs)?.text).toBe("Gmail: Relabel 1 thread");
    expect(describeAction(gmailPreviews.drafts_create, draftArgs)?.text).toBe("Gmail: Draft email");
  });

  it("rejects unknown arguments instead of rendering a custom preview", () => {
    expect(
      renderPreview(
        gmailPreviews.drafts_create,
        { to: ["a@example.com"], subject: "Hello", body: "Hi there", unexpected: true },
        "compact"
      )
    ).toBeNull();
  });

  it("returns null when threads_modify_labels args are malformed", () => {
    // thread_ids is min_length=1; an empty list fails the schema, so renderPreview returns null
    // and the caller shows raw JSON rather than a blank Arguments field.
    expect(renderPreview(gmailPreviews.threads_modify_labels, { thread_ids: [], add: ["x"] }, "compact")).toBeNull();
  });

  it("has no entry for read tools (self-descriptive args → raw JSON, no custom widget)", () => {
    expect("threads_list" in gmailPreviews).toBe(false);
  });
});
