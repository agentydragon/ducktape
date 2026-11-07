// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import type * as GmailClient from "../../gmail_client";
import { renderResultPreview } from "../result_entry";
import { gmailResultPreviews } from "./responses";

vi.mock("../../gmail_client", async (importOriginal) => ({
  ...(await importOriginal<typeof GmailClient>()),
  fetchGmailLabelNames: vi.fn().mockResolvedValue(new Map()),
}));

describe("gmailResultPreviews", () => {
  it("accepts threads_get for a full thread, in both variants", () => {
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

  it("accepts threads_get for a thread with no messages (minimal/metadata format)", () => {
    expect(renderResultPreview(gmailResultPreviews.threads_get, { id: "t1", snippet: "hi" }, "compact")).not.toBeNull();
  });

  it("accepts messages_get for a full message, in both variants", () => {
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

  it("shows the missing-subject fallback and thread link for a minimal thread", async () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    try {
      await act(async () => {
        root.render(
          createElement(
            MantineProvider,
            null,
            renderResultPreview(gmailResultPreviews.threads_get, { id: "t1", snippet: "hi" }, "compact")
          )
        );
      });
      expect(container.textContent).toContain("(no subject)");
      expect(container.textContent).toContain("hi");
      expect(container.querySelector("a")?.getAttribute("href")).toContain("#all/t1");
    } finally {
      await act(async () => root.unmount());
    }
  });
});
