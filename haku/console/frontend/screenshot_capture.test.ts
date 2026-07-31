// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { ScreenshotSession } from "./screenshot_capture";

// jsdom has no getDisplayMedia / real video decoding; each test stubs what it needs and
// restores afterward.
afterEach(() => vi.restoreAllMocks());

function fakeTrack() {
  const listeners: Record<string, (() => void)[]> = {};
  return {
    addEventListener: vi.fn((event: string, handler: () => void) => {
      (listeners[event] ??= []).push(handler);
    }),
    stop: vi.fn(),
    fireEnded: () => listeners.ended?.forEach((h) => h()),
  };
}

function stubGetDisplayMedia(track: ReturnType<typeof fakeTrack>) {
  const stream = { getVideoTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream;
  const getDisplayMedia = vi.fn().mockResolvedValue(stream);
  vi.stubGlobal("navigator", { ...navigator, mediaDevices: { getDisplayMedia } });
  vi.spyOn(HTMLVideoElement.prototype, "play").mockResolvedValue(undefined);
  return getDisplayMedia;
}

describe("ScreenshotSession.start", () => {
  it("becomes active on a successful getDisplayMedia grant, and posts the browser-tab hint", async () => {
    const track = fakeTrack();
    const getDisplayMedia = stubGetDisplayMedia(track);
    const session = new ScreenshotSession(() => {});
    expect(session.active).toBe(false);

    const result = await session.start();

    expect(result).toEqual({ ok: true });
    expect(session.active).toBe(true);
    expect(getDisplayMedia).toHaveBeenCalledWith({ video: { displaySurface: "browser" } });
  });

  it("is idempotent while already active — no second getDisplayMedia call", async () => {
    const getDisplayMedia = stubGetDisplayMedia(fakeTrack());
    const session = new ScreenshotSession(() => {});
    await session.start();

    const second = await session.start();

    expect(second).toEqual({ ok: true });
    expect(getDisplayMedia).toHaveBeenCalledTimes(1);
  });

  it("resolves ok:false with the browser's own reason when the operator declines the picker", async () => {
    vi.stubGlobal("navigator", {
      ...navigator,
      mediaDevices: { getDisplayMedia: vi.fn().mockRejectedValue(new Error("Permission denied")) },
    });
    const session = new ScreenshotSession(() => {});

    expect(await session.start()).toEqual({ ok: false, reason: "Permission denied" });
    expect(session.active).toBe(false);
  });

  it("resolves ok:false when the browser has no getDisplayMedia API", async () => {
    // jsdom's own default for navigator.mediaDevices varies by version — force absence rather
    // than relying on the ambient default, matching geolocation.test.ts's explicit-stub pattern.
    vi.stubGlobal("navigator", { ...navigator, mediaDevices: undefined });
    const session = new ScreenshotSession(() => {});

    expect(await session.start()).toEqual({
      ok: false,
      reason: "Screen capture is unavailable in this browser.",
    });
  });

  it("calls onEndedExternally and drops active when the track ends outside of stop()", async () => {
    const track = fakeTrack();
    stubGetDisplayMedia(track);
    const onEnded = vi.fn();
    const session = new ScreenshotSession(onEnded);
    await session.start();
    expect(session.active).toBe(true);

    track.fireEnded();

    expect(session.active).toBe(false);
    expect(onEnded).toHaveBeenCalledTimes(1);
  });
});

describe("ScreenshotSession.captureFrame", () => {
  it("returns null before any capture has started", () => {
    const session = new ScreenshotSession(() => {});
    expect(session.captureFrame(new DOMRect(0, 0, 100, 100))).toBeNull();
  });

  it("crops by the captured surface's pixel size relative to the viewport, not a blind DPR guess", async () => {
    stubGetDisplayMedia(fakeTrack());
    const session = new ScreenshotSession(() => {});
    await session.start();

    // A 2x-scaled capture (e.g. a HiDPI tab share) of a 1000x800 viewport.
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1000 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });
    Object.defineProperty(HTMLVideoElement.prototype, "videoWidth", { configurable: true, value: 2000 });
    Object.defineProperty(HTMLVideoElement.prototype, "videoHeight", { configurable: true, value: 1600 });
    const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage,
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue("data:image/png;base64,CROPPED");

    // The iframe occupies [100,50]..[600,450] in CSS pixels — should map to [200,100]..[1200,900]
    // in the 2x-scaled capture.
    const rect = new DOMRect(100, 50, 500, 400);
    const result = session.captureFrame(rect);

    expect(result).toBe("data:image/png;base64,CROPPED");
    expect(drawImage).toHaveBeenCalledWith(expect.any(HTMLVideoElement), 200, 100, 1000, 800, 0, 0, 1000, 800);
  });

  it("returns null once the video has no dimensions yet (capture not actually live)", async () => {
    stubGetDisplayMedia(fakeTrack());
    const session = new ScreenshotSession(() => {});
    await session.start();
    // jsdom's default videoWidth/videoHeight are 0 — captureFrame must not divide by them.

    expect(session.captureFrame(new DOMRect(0, 0, 100, 100))).toBeNull();
  });
});

describe("ScreenshotSession.stop", () => {
  it("stops every track and deactivates", async () => {
    const track = fakeTrack();
    stubGetDisplayMedia(track);
    const session = new ScreenshotSession(() => {});
    await session.start();

    session.stop();

    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(session.active).toBe(false);
  });
});
