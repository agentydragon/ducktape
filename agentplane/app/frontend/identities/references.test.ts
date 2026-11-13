import { create } from "@bufbuild/protobuf";
import { expect, it } from "vitest";

import { EnvironmentDescriptorSchema } from "../../../environments/v1/environment_service_pb";
import { IdentitySchema, PolicyBindingSchema } from "../../../policy/v1/policy_service_pb";
import { EnvironmentTargetSchema } from "../../../proto/identity_pb";
import { SessionSchema } from "../../../runner_gateway/dispatcher/api/session/session_dispatcher_pb";
import { identityBindings, remainingIdentityReferences } from "./references";

const identity = create(IdentitySchema, { identity: { namespace: "policies", name: "fake-agent", uid: "current" } });

function binding(name: string, subject: string, ownerUid: string | null) {
  return create(PolicyBindingSchema, {
    identity: { namespace: "policies", name, uid: `uid-${name}` },
    ownerReferences: ownerUid ? [{ kind: "Identity", name: subject, uid: ownerUid }] : [],
    spec: { subject: { identity: { name: subject } }, policySets: ["base"] },
  });
}

it("splits the bindings naming an identity into its own and those referencing it", () => {
  const split = identityBindings(identity, [
    binding("launch", "fake-agent", "current"),
    binding("standing-grant", "fake-agent", null),
    // Owned by an earlier incarnation of the same name, so it outlives this one's delete.
    binding("stale-owner", "fake-agent", "previous"),
    binding("other-agent", "other-agent", null),
    create(PolicyBindingSchema, {
      identity: { namespace: "policies", name: "account", uid: "uid-account" },
      spec: { subject: { namespace: "policies", name: "fake-agent" }, policySets: ["base"] },
    }),
  ]);
  expect(split.own.map((item) => item.identity?.name)).toEqual(["launch"]);
  expect(split.referencing.map((item) => item.identity?.name)).toEqual(["standing-grant", "stale-owner"]);
});

it("blocks cleanup for other Sandboxes, retained Sessions and unowned bindings", () => {
  const deleting = create(EnvironmentTargetSchema, { namespace: "tools", name: "one", uid: "one-uid" });
  const sandbox = (name: string, uid: string, identityName: string) =>
    create(EnvironmentDescriptorSchema, {
      target: { namespace: "tools", name, uid },
      identityName,
    });
  const sessions = [
    create(SessionSchema, { sessionId: "same-sandbox-session", identity: { name: "fake-agent", uid: "current" } }),
    create(SessionSchema, { sessionId: "other-agent-session", identity: { name: "other-agent", uid: "other" } }),
  ];
  expect(
    remainingIdentityReferences(
      identity,
      [binding("external", "fake-agent", null)],
      [sandbox("one", "one-uid", "fake-agent"), sandbox("two", "two-uid", "fake-agent")],
      sessions,
      deleting
    )
  ).toEqual(["PolicyBinding external", "Sandbox tools/two", "Session same-sandbox-session"]);
  expect(remainingIdentityReferences(identity, [], [sandbox("one", "one-uid", "fake-agent")], [], deleting)).toEqual(
    []
  );
});
