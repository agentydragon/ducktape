import { describe, expect, it } from "vitest";

import { toolActionDescription } from "../actions.ts";
import { renderPreview } from "../entry.tsx";
import { GMAIL_SERVER_ID } from "../server_ids.ts";
import { gmailPreviews } from "./requests.tsx";

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

  it("keeps custom previews and action text when nullable FastMCP arguments are explicitly null", () => {
    const relabelArgs = { thread_ids: ["t1"], add: ["Follow up"], remove: null };

    expect(renderPreview(gmailPreviews.threads_modify_labels, relabelArgs, "compact")).not.toBeNull();
    expect(toolActionDescription(GMAIL_SERVER_ID, "threads_modify_labels", relabelArgs)?.text).toBe(
      "Gmail: Relabel 1 thread"
    );
  });

  it("returns null when threads_modify_labels args are malformed", () => {
    // thread_ids is min_length=1; an empty list fails the schema, so renderPreview returns null
    // and the caller shows raw JSON rather than a blank Arguments field.
    expect(renderPreview(gmailPreviews.threads_modify_labels, { thread_ids: [], add: ["x"] }, "compact")).toBeNull();
  });

  it("renders every Gmail read tool with a widget", () => {
    expect(renderPreview(gmailPreviews.threads_get, { id: "t1" }, "compact")).not.toBeNull();
    expect(renderPreview(gmailPreviews.threads_list, { q: "from:alice" }, "detailed")).not.toBeNull();
    expect(renderPreview(gmailPreviews.messages_get, { id: "m1" }, "compact")).not.toBeNull();
  });

  it("has no entry for read tools with no useful preview (self-descriptive or empty args)", () => {
    expect("labels_list" in gmailPreviews).toBe(false);
  });

  it("has no entry for drafts_create — it's a combined widget (calls.tsx) instead", () => {
    expect("drafts_create" in gmailPreviews).toBe(false);
  });
});
