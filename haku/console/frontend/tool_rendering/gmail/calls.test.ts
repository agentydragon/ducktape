import { describe, expect, it } from "vitest";

import { toolActionDescription } from "../actions";
import { renderCallPreview } from "../call_entry";
import { GMAIL_SERVER_ID } from "../server_ids";
import { gmailCallPreviews } from "./calls";

const ARGS = { to: ["a@example.com"], subject: "Hello", body: "Hi there" };
const RESULT = { id: "r-1", message: { id: "m1", threadId: "t1" } };

function toCallToolResult(structuredContent: unknown) {
  return { content: [{ type: "text", text: JSON.stringify(structuredContent) }], isError: false, structuredContent };
}

describe("gmailCallPreviews.drafts_create", () => {
  it("renders the pending (arguments) view before the call has executed, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderCallPreview(gmailCallPreviews.drafts_create, ARGS, null, variant)).not.toBeNull();
    }
  });

  it("renders the finished (result) view once the draft has been created, in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderCallPreview(gmailCallPreviews.drafts_create, ARGS, toCallToolResult(RESULT), variant)
      ).not.toBeNull();
    }
  });

  it("falls back to the pending view when there is no successful result yet", () => {
    expect(
      renderCallPreview(
        gmailCallPreviews.drafts_create,
        ARGS,
        { content: [{ type: "text", text: "boom" }], isError: true },
        "compact"
      )
    ).not.toBeNull();
  });

  it("returns null when the arguments don't parse", () => {
    expect(renderCallPreview(gmailCallPreviews.drafts_create, { subject: "Hello" }, null, "compact")).toBeNull();
  });

  it("describes the action from the arguments", () => {
    expect(toolActionDescription(GMAIL_SERVER_ID, "drafts_create", ARGS)?.text).toBe("Gmail: Draft email");
  });
});
