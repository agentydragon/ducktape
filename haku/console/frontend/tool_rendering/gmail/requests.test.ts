import { describe, expect, it } from "vitest";

import { toolActionDescription } from "../actions";
import { renderPreview } from "../entry";
import { GMAIL_SERVER_ID } from "../server_ids";
import { gmailPreviews } from "./requests";

describe("gmailPreviews", () => {
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
});
