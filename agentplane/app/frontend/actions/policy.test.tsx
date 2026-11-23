// @vitest-environment happy-dom
import { fromJson } from "@bufbuild/protobuf";
import { act } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { cleanupMounted, mountWithMantine, renderWithMantine } from "../../../../util/frontend/testing/react";

import { CallerActionPolicySchema } from "../../../action_service/api/contracts/action_service_pb";
import { ActionPolicySection, ActionPolicyWatch } from "./policy";
import type { WatchHandlers } from "../transport/watch_stream";

const watch = vi.hoisted(() => ({ calls: [] as { url: string; handlers: WatchHandlers; stopped: boolean }[] }));
vi.mock("../transport/watch_stream", () => ({
  followWatch: (url: string, handlers: WatchHandlers) => {
    const call = { url, handlers, stopped: false };
    watch.calls.push(call);
    return () => {
      call.stopped = true;
    };
  },
}));

afterEach(async () => {
  await cleanupMounted();
  watch.calls.length = 0;
});

it("shows the generated effective policy in evaluation order", async () => {
  const policy = fromJson(CallerActionPolicySchema, {
    synced: true,
    autoApproveIf: [
      {
        binding: "sandbox",
        policySet: "reads",
        index: 1,
        policy: {
          argumentSchema: {
            actions: [{ group: "github", names: ["create_issue"] }],
            argumentSchema: { properties: { owner: { const: "test-owner" } } },
          },
        },
      },
    ],
  });
  const container = await renderWithMantine(<ActionPolicySection policy={policy} />);
  expect(container.textContent).toContain("sandbox · reads[1]");
  expect(container.textContent).toContain("github: create_issue");
  expect(container.textContent).toContain('"owner"');
});

it("distinguishes an empty policy, an unsynced policy, and an unavailable watch", async () => {
  const { container, rerender } = await mountWithMantine(
    <ActionPolicySection policy={fromJson(CallerActionPolicySchema, { synced: true })} />
  );
  expect(container.textContent).toContain("every Action from this ServiceAccount waits for the operator");
  await rerender(<ActionPolicySection policy={fromJson(CallerActionPolicySchema, {})} />);
  expect(container.textContent).toContain("watch has not synced");
  await rerender(<ActionPolicySection policy={null} error="unavailable" />);
  expect(container.textContent).toContain("unavailable");
});

it("uses the generated watch and drops the old subject when the target changes", async () => {
  const { container, rerender } = await mountWithMantine(
    <ActionPolicyWatch subject={{ namespace: "team", name: "old" }} fence="uid-1" />
  );
  const first = watch.calls[0]!;
  expect(first.url).toContain("subject.namespace=team");
  expect(first.url).toContain("subject.name=old");
  await act(async () =>
    first.handlers.onFrame({ actionPolicy: { subject: { namespace: "team", name: "old" }, synced: true } })
  );
  expect(container.textContent).toContain("every Action from this ServiceAccount waits for the operator");
  await rerender(<ActionPolicyWatch subject={{ namespace: "team", name: "new" }} fence="uid-2" />);
  expect(first.stopped).toBe(true);
  expect(container.textContent).not.toContain("every Action from this ServiceAccount waits for the operator");
  const second = watch.calls[1]!;
  expect(() => second.handlers.onFrame({ actionPolicy: { subject: { namespace: "team", name: "old" } } })).toThrow(
    "different subject"
  );
  await act(async () =>
    second.handlers.onFrame({ actionPolicy: { subject: { namespace: "team", name: "new" }, synced: false } })
  );
  expect(container.textContent).toContain("watch has not synced");
  await act(async () =>
    second.handlers.onConnection({ phase: "reconnecting", since: 0, attempt: 1, lastError: "offline" })
  );
  expect(container.textContent).toContain("offline");
  expect(container.textContent).not.toContain("watch has not synced");
});
