import { describe, expect, it } from "vitest";

import { renderResultPreview } from "../result_entry.tsx";
import { gmailResultPreviews } from "./responses.tsx";

describe("gmailResultPreviews", () => {
  it("has no entry for drafts_create — it's a combined widget (calls.tsx) instead", () => {
    expect("drafts_create" in gmailResultPreviews).toBe(false);
  });

  it("renders threads_get for a full thread, in both variants", () => {
    const thread = {
      id: "t1",
      snippet: "hello world",
      messages: [
        {
          id: "m1",
          threadId: "t1",
          labelIds: ["INBOX", "Label_1"],
          snippet: "hello world",
          payload: { headers: [{ name: "Subject", value: "Q3 planning" }] },
        },
      ],
    };
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderResultPreview(gmailResultPreviews.threads_get, thread, variant)).not.toBeNull();
    }
  });

  it("renders threads_get for a thread with no messages (minimal/metadata format)", () => {
    expect(renderResultPreview(gmailResultPreviews.threads_get, { id: "t1", snippet: "hi" }, "compact")).not.toBeNull();
  });

  it("renders threads_list, including an empty page, in both variants", () => {
    const page = {
      threads: [
        { id: "t1", snippet: "hello" },
        { id: "t2", snippet: "world" },
      ],
      nextPageToken: "np",
    };
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderResultPreview(gmailResultPreviews.threads_list, page, variant)).not.toBeNull();
    }
    expect(renderResultPreview(gmailResultPreviews.threads_list, { threads: [] }, "compact")).not.toBeNull();
  });

  it("renders messages_get for a full message, in both variants", () => {
    const message = {
      id: "m1",
      threadId: "t1",
      labelIds: ["INBOX"],
      snippet: "hello world",
      payload: { headers: [{ name: "Subject", value: "Q3 planning" }] },
    };
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderResultPreview(gmailResultPreviews.messages_get, message, variant)).not.toBeNull();
    }
  });
});
