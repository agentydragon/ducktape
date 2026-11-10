// @vitest-environment happy-dom
import { act } from "react";
import { expect, it, vi } from "vitest";
import { create } from "@bufbuild/protobuf";

import { answerApi, mount, send, sendUpserts } from "../actions/testing/harness";
import {
  ActionGroupSchema,
  ExecutorKind,
  McpLifecycle,
  McpUnavailableReason,
  type ActionGroup,
} from "../../../action_service/action_service_pb";
import {
  McpLinkageStatus,
  McpRefreshAction,
  McpServerLinkageSchema,
  type McpServerLinkage,
} from "../../../action_service/operator_action_service_pb";
import { McpServers } from "./mcp_servers";

const LINKAGES = "/upstream/actions/v1/operator/mcp-servers:watch";
const HEALTH = "/upstream/actions/v1/action-groups:watch";

/** Mounts the page, then pushes `linkages` and `groups` down its two streams. */
async function render(linkages: McpServerLinkage[] = [], groups: ActionGroup[] = []): Promise<HTMLDivElement> {
  const container = await mount(<McpServers />);
  await send(linkages, LINKAGES);
  await send(groups, HEALTH);
  return container;
}

function linkage(
  serverId: string,
  status: McpLinkageStatus = McpLinkageStatus.LINKED,
  refreshFailure?: { action: McpRefreshAction; error: string; attempts: number }
): McpServerLinkage {
  return create(McpServerLinkageSchema, {
    serverId,
    serverUrl: "https://mcp.example.test",
    status,
    scopes: [],
    refreshFailure,
  });
}

// Each button's label, and the request it sends.
it.each([
  ["Reconnect", "/upstream/actions/v1/operator/mcp-servers/github/linkage/start", {}],
  ["Disconnect", "/upstream/actions/v1/operator/mcp-servers/github/linkage/disconnect", undefined],
] as const)("spins only the clicked %s button until the operation fails", async (label, path, body) => {
  const rows = [linkage("github"), linkage("second-server")];
  let fail!: () => void;
  const failed = new Promise<Response>((answer) => {
    fail = () => answer(Response.json({ detail: "Operation failed" }, { status: 502 }));
  });
  const sent = answerApi(() => failed);
  const container = await render(rows);
  const buttons = [...container.querySelectorAll("button")];
  const clicked = buttons.find((button) => button.textContent === label);
  if (!clicked) throw new Error(`Missing ${label}`);
  await act(async () => clicked.click());
  expect(container.querySelectorAll("button[data-loading]")).toHaveLength(1);
  expect(clicked.hasAttribute("data-loading")).toBe(true);
  expect(buttons.every((button) => button.disabled)).toBe(true);
  await vi.waitFor(() => expect(sent).toMatchObject([{ method: "POST", path, body }]));

  await act(async () => fail());
  await vi.waitFor(() => expect(container.textContent).toContain("Operation failed"));
  expect(container.querySelectorAll("button[data-loading]")).toHaveLength(0);
  expect(buttons.every((button) => !button.disabled)).toBe(true);
});

it("renders independent linkage and health snapshots, then updates either without a refresh button", async () => {
  const sent = answerApi(() => undefined);
  const container = await mount(<McpServers />);
  expect(container.textContent).toContain("Loading MCP servers");
  expect(container.textContent).not.toContain("No MCP servers are configured");
  await send([linkage("example")], LINKAGES);
  expect(container.textContent).toContain("Loading MCP servers");
  await send(
    [
      create(ActionGroupSchema, {
        key: "example",
        title: "Example",
        description: "",
        executorKind: ExecutorKind.MCP,
        executorDescription: "Example",
        available: true,
        health: {
          state: McpLifecycle.AVAILABLE,
          failures: 0,
        },
      }),
    ],
    HEALTH
  );
  expect(container.textContent).toContain("OAuth linklinked");
  expect(container.textContent).toContain("Connectionavailable");
  await sendUpserts(
    [
      create(ActionGroupSchema, {
        key: "example",
        title: "Example",
        description: "",
        executorKind: ExecutorKind.MCP,
        executorDescription: "Example",
        available: false,
        health: {
          state: McpLifecycle.DISCONNECTED,
          reason: McpUnavailableReason.CONNECT_FAILED,
          detail: "Connection refused",
          failures: 1,
        },
      }),
    ],
    HEALTH
  );
  expect(container.textContent).toContain("Connectionconnect_failed");
  expect(container.textContent).toContain("Connection refused");
  expect(container.textContent).not.toContain("Refresh");
  expect(sent).toEqual([]);
});

it("shows a bearer-auth group with a live health badge and no link/disconnect buttons", async () => {
  const container = await render(
    [],
    [
      create(ActionGroupSchema, {
        key: "tana",
        title: "Tana",
        description: "Tana MCP tools",
        executorKind: ExecutorKind.MCP,
        executorDescription: "Tana MCP tools",
        available: true,
        health: {
          state: McpLifecycle.AVAILABLE,
          failures: 0,
        },
      }),
    ]
  );
  expect(container.textContent).toContain("tana");
  expect(container.textContent).toContain("available");
  const buttons = [...container.querySelectorAll("button")].map((button) => button.textContent);
  expect(buttons).not.toContain("Link account");
  expect(buttons).not.toContain("Disconnect");
});

it("labels a linked-but-disconnected server's two states and says why it cannot connect", async () => {
  const container = await render(
    [linkage("github")],
    [
      create(ActionGroupSchema, {
        key: "github",
        title: "GitHub",
        description: "Read access to public GitHub repositories.",
        executorKind: ExecutorKind.MCP,
        executorDescription: "Connected as Rai's GitHub account.",
        available: true,
        health: {
          state: McpLifecycle.DISCONNECTED,
          reason: McpUnavailableReason.CONNECT_FAILED,
          detail: "RuntimeError: Client failed to connect: All connection attempts failed",
          failures: 3,
        },
      }),
    ]
  );
  expect(container.textContent).toContain("OAuth linklinked");
  expect(container.textContent).toContain("Connectionconnect_failed");
  expect(container.textContent).toContain("RuntimeError: Client failed to connect: All connection attempts failed");
});

it("renders an oauth linkage with no matching health row exactly as before", async () => {
  const container = await render([linkage("kubernetes", McpLinkageStatus.UNLINKED)]);
  expect(container.textContent).toContain("kubernetes");
  expect(container.textContent).toContain("unlinked");
  const buttons = [...container.querySelectorAll("button")].map((button) => button.textContent);
  expect(buttons).toContain("Link account");
});

it("excludes a non-mcp (e.g. sandbox-kind) group from the list", async () => {
  const container = await render(
    [],
    [
      create(ActionGroupSchema, {
        key: "sandbox",
        title: "Sandbox",
        description: "Runs in-process, not over MCP.",
        executorKind: ExecutorKind.POD,
        executorDescription: "Stamped and exec'd by this service.",
        available: true,
      }),
    ]
  );
  expect(container.textContent).not.toContain("sandbox");
  expect(container.textContent).toContain("No MCP servers are configured");
});

it("shows a refused token refresh with the provider's error and what to do next", async () => {
  const container = await render([
    linkage("github", McpLinkageStatus.DEGRADED, {
      action: McpRefreshAction.RECONNECT,
      error: "the OAuth provider refused the token request: invalid_grant: Token is not active",
      attempts: 1,
    }),
  ]);
  expect(container.textContent).toContain(
    "Token refresh failed once, link the account again:the OAuth provider refused the token request: invalid_grant: Token is not active"
  );
});

it("does not repeat a linkage wait under a refresh failure that already explains it", async () => {
  const container = await render(
    [
      linkage("github", McpLinkageStatus.DEGRADED, {
        action: McpRefreshAction.RECONNECT,
        error: "test-only refusal",
        attempts: 1,
      }),
    ],
    [
      create(ActionGroupSchema, {
        key: "github",
        title: "GitHub",
        description: "Test MCP backend.",
        executorKind: ExecutorKind.MCP,
        executorDescription: "Linked operator account.",
        available: false,
        health: {
          state: McpLifecycle.DISCONNECTED,
          reason: McpUnavailableReason.LINKAGE_UNAVAILABLE,
          detail: "test-only linkage wait",
          failures: 1,
        },
      }),
    ]
  );
  expect(container.textContent).toContain("test-only refusal");
  expect(container.textContent).not.toContain("test-only linkage wait");
});
