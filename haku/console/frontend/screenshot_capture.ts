// Shell-side screen capture: a real `getDisplayMedia` tab/window/screen capture, held alive across
// requests, cropped to the iframe's own on-screen rect. Only the trusted top-level shell can call
// `getDisplayMedia` (the iframe has no `allow="display-capture"`; see docs/containment.md →
// `requestScreenshot`).
//
// Unlike geolocation's browser permission, `getDisplayMedia` has no persistent silent grant: the
// browser's own picker reappears for every NEW stream, and our standing grant only skips OUR
// confirm. So the session below is held alive deliberately — one picker interaction makes every
// subsequent capture an instant frame grab, until the operator stops sharing from browser chrome.

export type ScreenshotStartResult = { ok: true } | { ok: false; reason: string };

// One live `getDisplayMedia` stream, decoded into a hidden <video> so `captureFrame` can draw
// its current frame to a canvas on demand. `start()` MUST run inside a user-gesture call stack
// (an operator click) — browsers refuse `getDisplayMedia` otherwise.
export class ScreenshotSession {
  private stream: MediaStream | null = null;
  private video: HTMLVideoElement | null = null;

  // Called when the stream ends from OUTSIDE our own stop() — the operator's browser-native
  // "Stop sharing" control — so the shell can drop its "currently sharing" indicator.
  constructor(private readonly onEndedExternally: () => void) {}

  get active(): boolean {
    return this.stream !== null;
  }

  async start(): Promise<ScreenshotStartResult> {
    if (this.stream) return { ok: true };
    if (!navigator.mediaDevices?.getDisplayMedia) {
      return { ok: false, reason: "Screen capture is unavailable in this browser." };
    }
    let stream: MediaStream;
    try {
      // displaySurface: "browser" only hints the picker toward "this tab" — the operator still
      // chooses, and captureFrame's crop only makes sense if they picked this tab.
      stream = await navigator.mediaDevices.getDisplayMedia({ video: { displaySurface: "browser" } });
    } catch (e) {
      return { ok: false, reason: e instanceof Error ? e.message : String(e) };
    }
    const video = document.createElement("video");
    video.muted = true;
    video.srcObject = stream;
    await video.play();
    stream.getVideoTracks()[0]?.addEventListener("ended", () => {
      this.stream = null;
      this.video = null;
      this.onEndedExternally();
    });
    this.stream = stream;
    this.video = video;
    return { ok: true };
  }

  // Grab the live stream's current frame and crop it to `rect` (a CSS-pixel
  // getBoundingClientRect, e.g. the iframe's), returning a PNG data URL — or null if nothing is
  // live yet. Scales by the captured surface's actual pixel size vs. the viewport CSS size
  // (covers both device-pixel-ratio and any resolution the picker/browser applied), not a
  // blind `devicePixelRatio` guess.
  captureFrame(rect: DOMRect): string | null {
    const video = this.video;
    if (!video || video.videoWidth === 0) return null;
    const scaleX = video.videoWidth / window.innerWidth;
    const scaleY = video.videoHeight / window.innerHeight;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(rect.width * scaleX));
    canvas.height = Math.max(1, Math.round(rect.height * scaleY));
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(
      video,
      rect.left * scaleX,
      rect.top * scaleY,
      rect.width * scaleX,
      rect.height * scaleY,
      0,
      0,
      canvas.width,
      canvas.height
    );
    return canvas.toDataURL("image/png");
  }

  // The withdraw / unmount kill switch — stops sharing from our side (the operator's browser
  // also drops its "sharing this tab" indicator).
  stop(): void {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.video = null;
  }
}
