import { expect, it } from "vitest";

import type { SandboxView, ThreadView } from "./client";
import { archivedCount, groupThreads } from "./thread_groups";

function sandboxView(name: string, overrides: Partial<SandboxView> = {}): SandboxView {
  return {
    name,
    kind: "agent_sandbox",
    uid: `00000000-0000-4000-8000-${name.padStart(12, "0")}`,
    state: "running",
    created_at: "2026-01-01T00:00:00Z",
    operating_mode: "Running",
    service_account: { namespace: "agentplane-test", name },
    conditions: [],
    kubernetes_grants: [],
    kubernetes_grants_ready: true,
    kubernetes_grant_error: null,
    deleting: false,
    ...overrides,
  };
}

function threadView(overrides: Partial<ThreadView> & Pick<ThreadView, "id" | "sandbox" | "session_id">): ThreadView {
  return {
    sandbox_kind: "agent_sandbox",
    harness: "HARNESS_CLAUDE",
    model: "test-model",
    cwd: "/work",
    created_at: "2026-01-01T00:00:00Z",
    name: null,
    archived: false,
    last_cursor: 0,
    harness_state: "HARNESS_STATE_UNSPECIFIED",
    ...overrides,
  };
}

it("groups threads by sandbox, newest thread first fixing each group's order", () => {
  const newest = threadView({ id: "t-newest", sandbox: "sb-b", session_id: "s-1" });
  const middle = threadView({ id: "t-middle", sandbox: "sb-a", session_id: "s-2" });
  const oldest = threadView({ id: "t-oldest", sandbox: "sb-a", session_id: "s-3" });
  const sandboxes = {
    "agent_sandbox/sb-a": sandboxView("sb-a"),
    "agent_sandbox/sb-b": sandboxView("sb-b", { state: "suspended" }),
  };

  const groups = groupThreads([newest, middle, oldest], sandboxes, false);

  expect(groups.map((group) => group.sandboxName)).toEqual(["sb-b", "sb-a"]);
  expect(groups[1].threads).toEqual([middle, oldest]);
  expect(groups[0].sandbox?.state).toBe("suspended");
});

it("groups a thread whose sandbox is gone under a null sandbox rather than dropping it", () => {
  const orphan = threadView({ id: "t-orphan", sandbox: "sb-gone", session_id: "s-1" });

  const [group] = groupThreads([orphan], {}, false);

  expect(group.sandbox).toBeNull();
  expect(group.threads).toEqual([orphan]);
});

it("hides archived Threads without hiding their existing Sandbox", () => {
  const archived = threadView({ id: "t-1", sandbox: "sb-a", session_id: "s-1", archived: true });
  const kept = threadView({ id: "t-2", sandbox: "sb-b", session_id: "s-2" });
  const sandboxes = {
    "agent_sandbox/sb-a": sandboxView("sb-a"),
    "agent_sandbox/sb-b": sandboxView("sb-b"),
  };

  const hidden = groupThreads([archived, kept], sandboxes, false);
  expect(hidden.map((group) => group.sandboxName)).toEqual(["sb-b", "sb-a"]);
  expect(hidden[1].threads).toEqual([]);

  const shown = groupThreads([archived, kept], sandboxes, true);
  expect(shown.map((group) => group.sandboxName)).toEqual(["sb-a", "sb-b"]);
});

it("keeps same-name environments and their threads in separate groups", () => {
  const container = sandboxView("shared-name");
  const vm = sandboxView("shared-name", { kind: "kubevirt" });
  const threads = [
    threadView({ id: "container-thread", sandbox: "shared-name", session_id: "same-session" }),
    threadView({
      id: "vm-thread",
      sandbox: "shared-name",
      sandbox_kind: "kubevirt",
      session_id: "same-session",
    }),
  ];

  const groups = groupThreads(threads, { "agent_sandbox/shared-name": container, "kubevirt/shared-name": vm }, false);

  expect(groups.map((group) => [group.sandboxKind, group.sandbox, group.threads])).toEqual([
    ["agent_sandbox", container, [threads[0]]],
    ["kubevirt", vm, [threads[1]]],
  ]);
});

it("includes a provisioning Sandbox before any Thread exists", () => {
  const pending = sandboxView("test-provisioning", { state: "waiting_for_pod" });
  expect(groupThreads([], { [`${pending.kind}/${pending.name}`]: pending }, false)).toEqual([
    { sandboxName: pending.name, sandboxKind: pending.kind, sandbox: pending, threads: [] },
  ]);
});

it("counts archived threads across every sandbox regardless of visibility", () => {
  const threads = [
    threadView({ id: "t-1", sandbox: "sb-a", session_id: "s-1", archived: true }),
    threadView({ id: "t-2", sandbox: "sb-b", session_id: "s-2", archived: true }),
    threadView({ id: "t-3", sandbox: "sb-a", session_id: "s-3" }),
  ];

  expect(archivedCount(threads)).toBe(2);
});
