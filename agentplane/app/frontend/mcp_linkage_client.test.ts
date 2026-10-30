// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { StreamConnection } from "./stream_reader";
import {
  disconnectMcpLinkage,
  followMcpLinkages,
  startMcpLinkage,
  type McpLinkageWatchFrame,
} from "./mcp_linkage_client";
import { type ServedWatch, stubWatchFetch } from "./testing/watch";

let fetchMock: ReturnType<typeof stubWatchFetch>;
let frames: McpLinkageWatchFrame[];
let connections: StreamConnection[];
let stop: () => void = () => undefined;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = stubWatchFetch();
  frames = [];
  connections = [];
});

afterEach(() => {
  stop();
  frames = [];
  connections = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function watch(index = 0): ServedWatch {
  const served = fetchMock.served[index];
  if (served === undefined) throw new Error(`watch ${index} was not opened`);
  return served;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

it("starts linkage through the generated facade request and returns its authorization URL", async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    Response.json({ authorizationUrl: "https://provider.example/authorize?state=flow" })
  );

  await expect(startMcpLinkage("example docs", ["openid", "read"], fetcher)).resolves.toBe(
    "https://provider.example/authorize?state=flow"
  );
  const [input, init] = fetcher.mock.calls[0] ?? [];
  expect(input).toBe("/upstream/actions/v1/operator/mcp-servers/example%20docs/linkage/start");
  expect(init?.method).toBe("POST");
  expect(init?.headers).toMatchObject({ "Content-Type": "application/json" });
  expect(JSON.parse(String(init?.body))).toEqual({ scopes: ["openid", "read"] });
});

it("decodes disconnect's ProtoJSON linkage without the old OpenAPI model", async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    Response.json({
      linkage: {
        serverId: "example",
        serverUrl: "https://mcp.example.test",
        status: "MCP_LINKAGE_STATUS_DEGRADED",
        scopes: ["openid"],
        refreshFailure: {
          action: "MCP_REFRESH_ACTION_RECONNECT",
          error: "invalid_grant",
          attempts: 1,
        },
      },
    })
  );

  await expect(disconnectMcpLinkage("example", fetcher)).resolves.toEqual({
    server_id: "example",
    server_url: "https://mcp.example.test",
    status: "degraded",
    scopes: ["openid"],
    refresh_failure: { action: "reconnect", error: "invalid_grant", attempts: 1, retry_at: null },
  });
  const [input, init] = fetcher.mock.calls[0] ?? [];
  expect(input).toBe("/upstream/actions/v1/operator/mcp-servers/example/linkage/disconnect");
  expect(init?.method).toBe("POST");
  expect(init?.body).toBeUndefined();
});

it("replays linkage snapshots and decodes upserts from the generated watch frames", async () => {
  let opens = 0;
  stop = followMcpLinkages({
    onOpen: () => opens++,
    onFrame: (frame) => frames.push(frame),
    onConnection: (connection) => connections.push(connection),
  });
  await settle();
  expect(watch().url.pathname).toBe("/upstream/actions/v1/operator/mcp-servers:watch");

  watch().send({
    snapshot: {
      linkages: [
        {
          serverId: "example",
          serverUrl: "https://mcp.example.test",
          status: "MCP_LINKAGE_STATUS_LINKED",
          scopes: ["openid"],
        },
      ],
    },
  });
  watch().send({
    snapshot: {
      linkages: [
        { serverId: "second", serverUrl: "https://second.example.test", status: "MCP_LINKAGE_STATUS_UNLINKED" },
      ],
    },
  });
  watch().send({ snapshotComplete: {} });
  watch().send({
    upsert: {
      serverId: "example",
      serverUrl: "https://mcp.example.test",
      status: "MCP_LINKAGE_STATUS_EXPIRED",
      scopes: ["openid"],
    },
  });
  await settle();

  expect(frames).toEqual([
    {
      kind: "snapshot",
      linkages: [
        {
          server_id: "example",
          server_url: "https://mcp.example.test",
          status: "linked",
          scopes: ["openid"],
          refresh_failure: null,
        },
      ],
    },
    {
      kind: "snapshot",
      linkages: [
        {
          server_id: "second",
          server_url: "https://second.example.test",
          status: "unlinked",
          scopes: [],
          refresh_failure: null,
        },
      ],
    },
    { kind: "snapshot_complete" },
    {
      kind: "upsert",
      linkage: {
        server_id: "example",
        server_url: "https://mcp.example.test",
        status: "expired",
        scopes: ["openid"],
        refresh_failure: null,
      },
    },
  ]);
  expect(opens).toBe(1);
  expect(connections.map(({ phase }) => phase)).toEqual(["connecting", "live"]);
});
