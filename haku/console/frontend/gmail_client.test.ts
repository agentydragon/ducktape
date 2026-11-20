import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchGmailThreadPreview } from "./gmail_client";
import { callOperatorMcpTool } from "./mcp_client";

vi.mock("./mcp_client", () => ({ callOperatorMcpTool: vi.fn() }));

const callTool = vi.mocked(callOperatorMcpTool);

beforeEach(() => {
  callTool.mockReset();
});

describe("fetchGmailThreadPreview", () => {
  it("reads the subject and link using only the requested thread", async () => {
    callTool.mockResolvedValue({
      id: "t1",
      messages: [{ id: "m1", payload: { headers: [{ name: "subject", value: "Test subject" }] } }],
    });

    await expect(fetchGmailThreadPreview("t1")).resolves.toEqual({
      subject: "Test subject",
      gmail_url: "https://mail.google.com/mail/u/0/#all/t1",
    });
    expect(callTool.mock.calls).toEqual([["gmail__threads_get", { id: "t1", format: "metadata" }]]);
  });

  it("keeps the link when the provider omits the subject", async () => {
    callTool.mockResolvedValue({ id: "missing-content" });
    await expect(fetchGmailThreadPreview("missing-content")).resolves.toEqual({
      subject: null,
      gmail_url: "https://mail.google.com/mail/u/0/#all/missing-content",
    });
  });
});
