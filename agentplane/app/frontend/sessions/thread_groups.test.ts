import { expect, it } from "vitest";

import {
  SandboxPhase,
  OperatingMode,
  type EnvironmentDescriptor,
} from "../../../environments/v1/environment_service_pb";

import { testSandbox } from "../testing/sandbox";
import { groupThreads } from "./thread_groups";
import { threadView } from "./testing/state";

function sandbox(name: string, uid = `00000000-0000-4000-8000-${name.padStart(12, "0")}`): EnvironmentDescriptor {
  const value = testSandbox(name, uid);
  value.observedState!.operatingMode = OperatingMode.RUNNING;
  value.observedState!.phase = SandboxPhase.CREATING;
  return value;
}

function thread(id: string, sandboxName: string, uid: string, archived = false) {
  return threadView({
    metadata: { threadId: id, archived },
    summary: { sandbox: { name: sandboxName, uid } },
  });
}

it("groups threads by exact Sandbox identity, newest thread first fixing each group's order", () => {
  const newest = thread("t-newest", "sb-b", `00000000-0000-4000-8000-${"sb-b".padStart(12, "0")}`);
  const middle = thread("t-middle", "sb-a", `00000000-0000-4000-8000-${"sb-a".padStart(12, "0")}`);
  const oldest = thread("t-oldest", "sb-a", `00000000-0000-4000-8000-${"sb-a".padStart(12, "0")}`);
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
  const archived = thread("t-1", "sb-a", `00000000-0000-4000-8000-${"sb-a".padStart(12, "0")}`, true);
  const kept = thread("t-2", "sb-b", `00000000-0000-4000-8000-${"sb-b".padStart(12, "0")}`);
  const sandboxes = { "sb-a": sandbox("sb-a"), "sb-b": sandbox("sb-b") };

  const hidden = groupThreads([archived, kept], sandboxes, false, null);
  expect(hidden.map((group) => group.sandboxName)).toEqual(["sb-b", "sb-a"]);
  expect(hidden[1].threads).toEqual([]);

  const shown = groupThreads([archived, kept], sandboxes, true, null);
  expect(shown.map((group) => group.sandboxName)).toEqual(["sb-a", "sb-b"]);
});

it("keeps the open Thread in its group even when it is archived and archived Threads are hidden", () => {
  const uid = `00000000-0000-4000-8000-${"sb-a".padStart(12, "0")}`;
  const open = thread("t-1", "sb-a", uid, true);
  const other = thread("t-2", "sb-a", uid, true);
  const kept = thread("t-3", "sb-a", uid);

  const groups = groupThreads([open, other, kept], { "sb-a": sandbox("sb-a") }, false, "t-1");

  expect(groups.map((group) => group.threads)).toEqual([[open, kept]]);
});

it("does not attach retained history to a recreated Sandbox with the same name", () => {
  const oldThread = thread("old", "sb-a", "old-sandbox-uid");
  const currentSandbox = sandbox("sb-a");

  const groups = groupThreads([oldThread], { "sb-a": currentSandbox }, false, null);

  expect(groups.map((group) => [group.sandboxName, group.sandbox, group.threads])).toEqual([
    ["sb-a", null, [oldThread]],
    ["sb-a", currentSandbox, []],
  ]);
});

it("does not attach history without a nonempty UID to a live Sandbox", () => {
  const legacyThread = thread("legacy", "sb-a", "");
  const currentSandbox = sandbox("sb-a");

  const groups = groupThreads([legacyThread], { "sb-a": currentSandbox }, false, null);

  expect(groups.map((group) => [group.sandboxName, group.sandbox, group.threads])).toEqual([
    ["sb-a", null, [legacyThread]],
    ["sb-a", currentSandbox, []],
  ]);
});
