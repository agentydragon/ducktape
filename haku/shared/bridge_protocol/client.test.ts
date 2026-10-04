import { afterEach, describe, expect, it, vi } from "vitest";

import { createBridgeClient } from "@haku/console-bridge";
import type { GeoPosition } from "@haku/console-bridge/protocol";

// Behavior test for the shared @haku/console-bridge client, which this repo owns and haku-state's
// iframe UI consumes. The console is the shell, not the iframe, so it never uses these helpers in
// production — this exercises the linked package end-to-end (import resolution + the postMessage
// request/reply correlation) so haku-state can adopt it safely. postMessage is spied, so nothing
// leaves jsdom: replies come from dispatching the event the client listens for.
const SHELL = "https://shell.example";
const { openLink, requestLaunch, requestGeolocation, watchGeolocation, notifyRouteChanged, requestScreenshot } =
  createBridgeClient(SHELL);

const POSITION: GeoPosition = {
  latitude: 37.7749,
  longitude: -122.4194,
  accuracy: 12,
  altitude: null,
  altitudeAccuracy: null,
  heading: null,
  speed: null,
  timestamp: 1_700_000_000_000,
};

function shellReply(data: Record<string, unknown>): void {
  window.dispatchEvent(new MessageEvent("message", { origin: SHELL, data }));
}

afterEach(() => vi.restoreAllMocks());

describe("openLink", () => {
  it("posts openLink to the trusted origin and resolves the matching-url verdict", async () => {
    const post = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
    const pending = openLink("https://example.com/x");
    expect(post.mock.calls[0][0]).toEqual({ type: "openLink", url: "https://example.com/x" });
    expect(post.mock.calls[0][1]).toBe(SHELL); // only ever posted to the trusted origin

    shellReply({ type: "openLinkResult", url: "https://example.com/x", opened: true });
    expect(await pending).toEqual({
      type: "openLinkResult",
      url: "https://example.com/x",
      opened: true,
      reason: undefined,
    });
  });
});

describe("requestLaunch", () => {
  it("posts requestLaunch and resolves the matching-id reply", async () => {
    const post = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
    const pending = requestLaunch("do the thing");
    const sent = post.mock.calls[0][0] as { type: string; id: string; prompt: string };
    expect(sent.type).toBe("requestLaunch");
    expect(sent.prompt).toBe("do the thing");

    shellReply({ type: "launchResult", id: sent.id, ok: true, sessionUrl: "https://claude.ai/s/1" });
    const result = await pending;
    expect(result.ok).toBe(true);
    expect(result.sessionUrl).toBe("https://claude.ai/s/1");
  });
});

describe("requestGeolocation", () => {
  it("posts the options and resolves the matching-id ok reply", async () => {
    const post = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
    const pending = requestGeolocation({ enableHighAccuracy: true });
    const sent = post.mock.calls[0][0] as { type: string; id: string; options: unknown };
    expect(sent.type).toBe("requestGeolocation");
    expect(sent.options).toEqual({ enableHighAccuracy: true });

    shellReply({ type: "geolocationResult", id: sent.id, ok: true, position: POSITION });
    const result = await pending;
    expect(result.ok).toBe(true);
    expect(result.position).toEqual(POSITION);
  });

  it("ignores a foreign origin and a mismatched id, then resolves the real reply", async () => {
    const post = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
    const pending = requestGeolocation();
    const { id } = post.mock.calls[0][0] as { id: string };

    // Foreign origin — a spoofed reply must not resolve the promise.
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://evil.example",
        data: { type: "geolocationResult", id, ok: true, position: POSITION },
      })
    );
    // Right origin, wrong id — a stale reply for another request must not resolve this one.
    shellReply({ type: "geolocationResult", id: "some-other-id", ok: true, position: POSITION });
    // The genuine reply: operator declined → ok:false, code 1 (PERMISSION_DENIED).
    shellReply({ type: "geolocationResult", id, ok: false, code: 1, reason: "declined" });

    const result = await pending;
    expect(result.ok).toBe(false);
    expect(result.code).toBe(1);
  });
});

describe("requestScreenshot", () => {
  it("posts requestScreenshot and resolves the matching-id ok reply", async () => {
    const post = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
    const pending = requestScreenshot();
    const sent = post.mock.calls[0][0] as { type: string; id: string };
    expect(sent.type).toBe("requestScreenshot");

    shellReply({ type: "screenshotResult", id: sent.id, ok: true, imageDataUrl: "data:image/png;base64,AAAA" });
    const result = await pending;
    expect(result.ok).toBe(true);
    expect(result.imageDataUrl).toBe("data:image/png;base64,AAAA");
  });

  it("resolves ok:false with a reason on decline", async () => {
    const post = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
    const pending = requestScreenshot();
    const { id } = post.mock.calls[0][0] as { id: string };

    shellReply({ type: "screenshotResult", id, ok: false, reason: "declined" });
    const result = await pending;
    expect(result).toEqual({ type: "screenshotResult", id, ok: false, imageDataUrl: undefined, reason: "declined" });
  });
});

describe("notifyRouteChanged", () => {
  it("starts automatic title mirroring and follows later title changes", async () => {
    const post = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
    document.title = "Runs · Haku";

    notifyRouteChanged("/runs");
    expect(post.mock.calls[0]).toEqual([{ type: "routeChanged", path: "/runs" }, SHELL]);
    expect(post.mock.calls[1]).toEqual([{ type: "titleChanged", title: "Runs · Haku" }, SHELL]);

    document.title = "Garden · Haku";
    await vi.waitFor(() => {
      expect(post.mock.calls.at(-1)).toEqual([{ type: "titleChanged", title: "Garden · Haku" }, SHELL]);
    });
  });
});

describe("watchGeolocation", () => {
  it("streams each fix to onFix and stops posting/listening after stop()", () => {
    const post = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
    const fixes: boolean[] = [];
    const stop = watchGeolocation((fix) => fixes.push(fix.ok));
    const { id } = post.mock.calls[0][0] as { id: string };
    expect((post.mock.calls[0][0] as { type: string }).type).toBe("startGeolocationWatch");

    shellReply({ type: "geolocationResult", id, ok: true, position: POSITION });
    shellReply({ type: "geolocationResult", id, ok: true, position: POSITION });
    expect(fixes).toEqual([true, true]);

    stop();
    expect(post.mock.calls.at(-1)?.[0]).toEqual({ type: "stopGeolocationWatch", id });
    // A fix delivered after stop() must be ignored (listener removed).
    shellReply({ type: "geolocationResult", id, ok: true, position: POSITION });
    expect(fixes).toEqual([true, true]);
  });
});
