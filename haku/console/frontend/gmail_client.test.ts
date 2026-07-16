import { describe, expect, it } from "vitest";

import { gmailThreadPreview } from "./gmail_client.ts";
import type { McpToolResultFor } from "./mcp_tool_result_schema.ts";

describe("gmailThreadPreview", () => {
  it("derives preview fields from threads_get and labels_list results", () => {
    const thread = {
      id: "t1",
      messages: [
        {
          id: "m1",
          labelIds: ["Label_2", "UNKNOWN", "Label_1"],
          snippet: "hello world",
          payload: { headers: [{ name: "subject", value: "Test subject" }] },
        },
      ],
    } satisfies McpToolResultFor<"gmail", "threads_get">;

    expect(
      gmailThreadPreview(
        "t1",
        thread,
        new Map([
          ["Label_1", "Alpha"],
          ["Label_2", "Zulu"],
        ])
      )
    ).toEqual({
      subject: "Test subject",
      snippet: "hello world",
      current_label_names: ["Alpha", "Zulu"],
      gmail_url: "https://mail.google.com/mail/u/0/#all/t1",
    });
  });

  it("keeps the display fallbacks for an empty thread", () => {
    expect(gmailThreadPreview("missing-content", { id: "missing-content" }, new Map())).toEqual({
      subject: null,
      snippet: "",
      current_label_names: [],
      gmail_url: "https://mail.google.com/mail/u/0/#all/missing-content",
    });
  });
});
