// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";

import { getSystemStatus } from "./system_status_client";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("decodes the generated AppService ProtoJSON response into the system status view", async () => {
  const paths: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      paths.push(new URL(input instanceof Request ? input.url : String(input), "http://agentplane.test").pathname);
      return Response.json({
        components: [
          {
            name: "Healthy component",
            state: "SYSTEM_COMPONENT_STATE_OK",
            checkedAt: "2026-01-01T00:00:00Z",
          },
          {
            name: "Down component",
            state: "SYSTEM_COMPONENT_STATE_DOWN",
            detail: "UNAVAILABLE: fixture connection refused",
            checkedAt: "2026-01-01T00:00:00Z",
          },
        ],
      });
    })
  );

  await expect(getSystemStatus()).resolves.toEqual([
    {
      name: "Healthy component",
      state: "ok",
      detail: null,
      checked_at: "2026-01-01T00:00:00Z",
    },
    {
      name: "Down component",
      state: "down",
      detail: "UNAVAILABLE: fixture connection refused",
      checked_at: "2026-01-01T00:00:00Z",
    },
  ]);
  expect(paths).toEqual(["/upstream/app/v1/system/status"]);
});

it("treats an omitted components field as an empty list", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({}))
  );
  await expect(getSystemStatus()).resolves.toEqual([]);
});
