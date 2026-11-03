import { expect, it } from "vitest";

import type { ThreadView } from "../schema";
import {
  SandboxPhase,
  OperatingMode,
  type EnvironmentDescriptor,
} from "../../../environments/v1/environment_service_pb";

import { testSandbox } from "../testing/sandbox";
import { groupThreads } from "./thread_groups";

function sandbox(name: string, overrides: Partial<EnvironmentDescriptor> = {}): EnvironmentDescriptor {
  const value = testSandbox(name, `00000000-0000-4000-8000-${name.padStart(12, "0")}`);
  value.observedState!.operatingMode = OperatingMode.RUNNING;
  value.observedState!.phase = SandboxPhase.CREATING;
  return Object.assign(value, overrides);
}

function threadView(overrides: Partial<ThreadView> & Pick<ThreadView, "id" | "sandbox">): ThreadView {
  return {
    harness: "HARNESS_CLAUDE",
    model: "test-model",
    cwd: "/work",
    created_at: "2026-01-01T00:00:00Z",
    name: null,
    archived: false,
    last_cursor: 0,
    sandbox_uid: null,
    last_event_at: null,
    last_model_activity_at: null,
    idle_since: null,
    harness_state: "HARNESS_STATE_UNSPECIFIED",
    setup_state: null,
    launch_outcome: null,
    active_turn_id: null,
    reasoning_effort: null,
    feed_status: null,
    last_turn_status: null,
    ...overrides,
  };
}

it("groups threads by sandbox, newest thread first fixing each group's order", () => {
  const newest = threadView({ id: "t-newest", sandbox: "sb-b" });
  const middle = threadView({ id: "t-middle", sandbox: "sb-a" });
  const oldest = threadView({ id: "t-oldest", sandbox: "sb-a" });
  const suspended = sandbox("sb-b");
  suspended.observedState!.operatingMode = OperatingMode.SUSPENDED;
  suspended.observedState!.phase = SandboxPhase.SUSPENDED;
  const sandboxes = { "sb-a": sandbox("sb-a"), "sb-b": suspended };

  const groups = groupThreads([newest, middle, oldest], sandboxes, false, null);

  expect(groups.map((group) => group.sandboxName)).toEqual(["sb-b", "sb-a"]);
  expect(groups[1].threads).toEqual([middle, oldest]);
  expect(groups[0].sandbox?.observedState?.operatingMode).toBe(OperatingMode.SUSPENDED);
});

it("hides archived Threads without hiding their existing Sandbox", () => {
  const archived = threadView({ id: "t-1", sandbox: "sb-a", archived: true });
  const kept = threadView({ id: "t-2", sandbox: "sb-b" });
  const sandboxes = { "sb-a": sandbox("sb-a"), "sb-b": sandbox("sb-b") };

  const hidden = groupThreads([archived, kept], sandboxes, false, null);
  expect(hidden.map((group) => group.sandboxName)).toEqual(["sb-b", "sb-a"]);
  expect(hidden[1].threads).toEqual([]);

  const shown = groupThreads([archived, kept], sandboxes, true, null);
  expect(shown.map((group) => group.sandboxName)).toEqual(["sb-a", "sb-b"]);
});

it("keeps the open Thread in its group even when it is archived and archived Threads are hidden", () => {
  const open = threadView({ id: "t-1", sandbox: "sb-a", archived: true });
  const other = threadView({ id: "t-2", sandbox: "sb-a", archived: true });
  const kept = threadView({ id: "t-3", sandbox: "sb-a" });

  const groups = groupThreads([open, other, kept], { "sb-a": sandbox("sb-a") }, false, "t-1");

  expect(groups.map((group) => group.threads)).toEqual([[open, kept]]);
});
