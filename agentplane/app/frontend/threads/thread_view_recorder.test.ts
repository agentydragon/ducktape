// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";

import { threadViewRecorder } from "./thread_view_recorder";

describe("Agentplane thread view User Timing", () => {
  afterEach(() => {
    delete window.__agentplaneThreadViewMarksEnabled;
    vi.unstubAllGlobals();
  });

  it("uses the browser test harness opt-in and preserves the Agentplane mark prefix", () => {
    const mark = vi.fn();
    vi.stubGlobal("performance", { mark, now: () => 10 });
    threadViewRecorder.mark({ kind: "load-older" });
    expect(mark).not.toHaveBeenCalled();

    window.__agentplaneThreadViewMarksEnabled = true;
    threadViewRecorder.mark({ kind: "follow", following: false, reason: "wheel-up" });
    expect(mark).toHaveBeenCalledWith("agentplane:thread-view:follow", {
      detail: { kind: "follow", following: false, reason: "wheel-up" },
    });
  });
});
