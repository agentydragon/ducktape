import { describe, expect, it, vi } from "vitest";

import { detailMessage, getSession, listSessionEvents } from "./api";

describe("detailMessage", () => {
  it("takes the string an HTTPException carries", () => {
    expect(detailMessage({ detail: "state differs" }, "fallback")).toBe("state differs");
  });

  it("joins the issues of a rejected request body", () => {
    expect(
      detailMessage({ detail: [{ msg: "Field required" }, { msg: "Input should be a string" }] }, "fallback")
    ).toBe("Field required; Input should be a string");
  });

  it("falls back for a body that says nothing usable", () => {
    expect(detailMessage(null, "fallback")).toBe("fallback");
    expect(detailMessage({ detail: [] }, "fallback")).toBe("fallback");
  });
});

describe("listSessionEvents", () => {
  it("requests a bounded newest-first page by default and preserves the opaque cursor", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ data: [], has_more: false, first_id: null, last_id: null }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
    );
    vi.stubGlobal("fetch", fetchMock);
    try {
      await listSessionEvents("session/one", "event-id:opaque");
      const requestUrl = new URL(String(fetchMock.mock.calls[0]?.[0]), "https://sessions.example");
      expect(requestUrl.pathname).toBe("/v1/code/sessions/session%2Fone/events");
      expect(requestUrl.searchParams.get("limit")).toBe("100");
      expect(requestUrl.searchParams.get("sort_order")).toBe("desc");
      expect(requestUrl.searchParams.get("cursor")).toBe("event-id:opaque");

      await listSessionEvents("session/one", "event-id:older", "asc");
      const catchUpUrl = new URL(String(fetchMock.mock.calls[1]?.[0]), "https://sessions.example");
      expect(catchUpUrl.searchParams.get("sort_order")).toBe("asc");
      expect(catchUpUrl.searchParams.get("cursor")).toBe("event-id:older");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("getSession", () => {
  it("fetches the latest session summary by encoded ID and forwards cancellation", async () => {
    const summary = {
      id: "session/one",
      title: "Updated from the change feed",
      status: "paused",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-02T00:00:00Z",
      last_event_at: "2026-01-02T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ session: summary }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    try {
      await expect(getSession(summary.id, controller.signal)).resolves.toEqual({ session: summary });
      const [path, options] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(new URL(path, "https://sessions.example").pathname).toBe("/v1/code/sessions/session%2Fone");
      expect(options.signal).toBe(controller.signal);
      expect(options.cache).toBe("no-store");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
