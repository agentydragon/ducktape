import { create } from "@bufbuild/protobuf";
import { expect, it } from "vitest";

import { IdentitySchema, PolicyBindingSchema } from "../../../policy/v1/policy_service_pb";
import { identityBindings } from "./references";

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
