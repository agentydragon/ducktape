import { create } from "@bufbuild/protobuf";
import { expect, it } from "vitest";

import { CredentialSchema, type Credential } from "../../../egress/egress_rules_pb";
import { PolicyBindingSchema, PolicySetSchema, type PolicySet } from "../../../policy/v1/policy_service_pb";
import { kubernetesAccounts } from "./accounts";

function set(name: string, credentials: string[]): [string, PolicySet] {
  return [
    name,
    create(PolicySetSchema, {
      identity: { name },
      spec: {
        rules: [
          ...credentials.map((credential) => ({
            type: "egress",
            hosts: ["fake.invalid"],
            credentialRef: { name: credential },
          })),
          { type: "egress", hosts: ["no-credential.invalid"] },
          { type: "action_auto_approve", policy: { type: "exact_actions" } },
        ],
      },
    }),
  ];
}
const binding = (...sets: string[]) => create(PolicyBindingSchema, { spec: { policySets: sets } });
const credentials = new Map<string, Credential>([
  [
    "impersonate-shared",
    create(CredentialSchema, {
      name: "impersonate-shared",
      kubernetesServiceAccount: { namespace: "fake-accounts", name: "shared" },
    }),
  ],
  [
    "impersonate-temporary",
    create(CredentialSchema, {
      name: "impersonate-temporary",
      kubernetesServiceAccount: { namespace: "fake-accounts", name: "temporary" },
    }),
  ],
  ["secret-token", create(CredentialSchema, { name: "secret-token" })],
]);
const sets = new Map([
  set("shared-use", ["impersonate-shared", "secret-token"]),
  set("temporary-use", ["impersonate-temporary", "impersonate-shared"]),
  set("unknown-credential", ["not-in-view"]),
]);

it("collects each impersonated account once across bindings and sets", () => {
  expect(
    kubernetesAccounts([binding("shared-use"), binding("temporary-use", "shared-use")], sets, credentials)
  ).toEqual({ accounts: ["fake-accounts/shared", "fake-accounts/temporary"], complete: true });
});

it.each([
  ["a missing set", [binding("missing-set", "shared-use")], credentials],
  ["a credential out of view", [binding("unknown-credential", "shared-use")], credentials],
])("is incomplete with %s", (_case, bindings, inventory) => {
  expect(kubernetesAccounts(bindings, sets, inventory)).toEqual({
    accounts: ["fake-accounts/shared"],
    complete: false,
  });
});

it("is incomplete before the credential inventory arrives", () => {
  expect(kubernetesAccounts([binding("shared-use")], sets, null)).toEqual({ accounts: [], complete: false });
});

it("reaches nothing without bindings", () => {
  expect(kubernetesAccounts([], sets, credentials)).toEqual({ accounts: [], complete: true });
});
