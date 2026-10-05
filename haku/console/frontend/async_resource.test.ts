// @vitest-environment happy-dom
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { useAsyncResource, type AsyncResource } from "./async_resource";

describe("useAsyncResource", () => {
  it("keeps authoritative data when stale loads settle or refresh fails", async () => {
    let settle: (value: string) => void = () => undefined;
    const pending = new Promise<string>((resolve) => (settle = resolve));
    const load = vi.fn().mockReturnValueOnce(pending).mockRejectedValueOnce(new Error("offline"));
    const resource: { current: AsyncResource<string> | null } = { current: null };
    const root = createRoot(document.createElement("div"));
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    function Harness() {
      resource.current = useAsyncResource(load);
      return null;
    }
    act(() => root.render(createElement(Harness)));
    await vi.waitFor(() => expect(load).toHaveBeenCalledOnce());
    act(() => resource.current?.update("authoritative"));
    await act(async () => {
      settle("stale");
      await pending;
    });
    expect(resource.current?.data).toBe("authoritative");
    act(() => resource.current?.refresh());
    await vi.waitFor(() => expect(resource.current?.error).toBe("offline"));
    expect(resource.current?.data).toBe("authoritative");
    act(() => root.unmount());
  });
});
