import { afterEach, describe, expect, it, vi } from "vitest";

import { sendFeedback } from "./client.ts";

describe("sendFeedback", () => {
  afterEach(() => vi.unstubAllGlobals());

  function stubOk() {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true } as Response));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }
  const bodyOf = (fetchMock: ReturnType<typeof vi.fn>) =>
    JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);

  it("posts the note text with a null item_id and no page/selection when no context is given", async () => {
    const fetchMock = stubOk();
    await sendFeedback("just a note");
    expect(fetchMock).toHaveBeenCalledWith("/api/trace/feedback", expect.objectContaining({ method: "POST" }));
    expect(bodyOf(fetchMock)).toEqual({ text: "just a note", item_id: null });
  });

  it("includes the page and selected text when a context is supplied", async () => {
    const fetchMock = stubOk();
    await sendFeedback("this page looks bad", undefined, { page: "#/runs", selection: "6 scanned · 2 skipped" });
    expect(bodyOf(fetchMock)).toEqual({
      text: "this page looks bad",
      item_id: null,
      page: "#/runs",
      selection: "6 scanned · 2 skipped",
    });
  });

  it("sends the page but omits selection when nothing was selected", async () => {
    const fetchMock = stubOk();
    await sendFeedback("note", undefined, { page: "#/inbox", selection: null });
    expect(bodyOf(fetchMock)).toEqual({ text: "note", item_id: null, page: "#/inbox" });
  });
});
