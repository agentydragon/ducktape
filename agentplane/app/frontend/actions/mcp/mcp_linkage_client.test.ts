// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { StreamConnection } from "../../transport/stream_reader";
import { disconnectMcpLinkage, followMcpLinkages, startMcpLinkage } from "./mcp_linkage_client";
import { type ServedWatch, stubWatchFetch } from "../../testing/watch";
import {
  McpLinkageStatus,
  McpRefreshAction,
  McpServerLinkageSchema,
  WatchMcpServersResponseSchema,
  type WatchMcpServersResponse,
} from "../../../../action_service/api/contracts/operator_action_service_pb";
import { create, toJson } from "@bufbuild/protobuf";

let fetchMock: ReturnType<typeof stubWatchFetch>;
let frames: WatchMcpServersResponse[];
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

it("returns disconnect's generated linkage", async () => {
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

  await expect(disconnectMcpLinkage("example", fetcher)).resolves.toEqual(
    create(McpServerLinkageSchema, {
      serverId: "example",
      serverUrl: "https://mcp.example.test",
      status: McpLinkageStatus.DEGRADED,
      scopes: ["openid"],
      refreshFailure: { action: McpRefreshAction.RECONNECT, error: "invalid_grant", attempts: 1 },
    })
  );
  const [input, init] = fetcher.mock.calls[0] ?? [];
  expect(input).toBe("/upstream/actions/v1/operator/mcp-servers/example/linkage/disconnect");
  expect(init?.method).toBe("POST");
  expect(init?.body).toBeUndefined();
});

it("replays generated linkage snapshots and upserts", async () => {
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

  expect(frames.map((frame) => toJson(WatchMcpServersResponseSchema, frame))).toEqual([
    {
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
    },
    {
      snapshot: {
        linkages: [
          { serverId: "second", serverUrl: "https://second.example.test", status: "MCP_LINKAGE_STATUS_UNLINKED" },
        ],
      },
    },
    { snapshotComplete: {} },
    {
      upsert: {
        serverId: "example",
        serverUrl: "https://mcp.example.test",
        status: "MCP_LINKAGE_STATUS_EXPIRED",
        scopes: ["openid"],
      },
    },
  ]);
  expect(opens).toBe(1);
  expect(connections.map(({ phase }) => phase)).toEqual(["connecting", "live"]);
});

it("preserves an unknown linkage enum value for presentation", async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    Response.json({ linkage: { serverId: "example", serverUrl: "https://mcp.example.test", status: 91 } })
  );

  expect((await disconnectMcpLinkage("example", fetcher)).status).toBe(91);
});

it("rejects a missing disconnect linkage", async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => Response.json({}));

  await expect(disconnectMcpLinkage("example", fetcher)).rejects.toThrow("omitted disconnected MCP linkage");
});
