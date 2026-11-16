import { create } from "@bufbuild/protobuf";
import { afterEach, expect, it, vi } from "vitest";

import { IdentitySchema } from "../../../policy/v1/policy_service_pb";
import { deleteIdentity } from "./client";

const identity = create(IdentitySchema, { identity: { namespace: "fake-policies", name: "fake agent", uid: "uid-1" } });
afterEach(() => vi.unstubAllGlobals());

function answer(response: Response): ReturnType<typeof vi.fn> {
  const fetch = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

it("deletes the exact incarnation through the generated facade route", async () => {
  const fetch = answer(Response.json({}));
  expect(await deleteIdentity(identity)).toEqual({ case: "deleted" });
  expect(fetch).toHaveBeenCalledWith("/upstream/policy/v1/identities/fake-policies/fake%20agent?uid=uid-1", {
    method: "DELETE",
  });
});

it("names the bindings and credentials a refusal's PreconditionFailure lists", async () => {
  answer(
    Response.json(
      {
        code: 9,
        message: "refused",
        details: [
          { "@type": "type.googleapis.com/google.rpc.ErrorInfo", reason: "other" },
          {
            "@type": "type.googleapis.com/google.rpc.PreconditionFailure",
            violations: [
              { type: "PolicyBinding", subject: "standing-grant", description: "names it" },
              { type: "WorkloadCredential", subject: "fake-workloads/standing-agent", description: "acts as it" },
              { type: "Sandbox", subject: "not-a-reference" },
            ],
          },
        ],
      },
      { status: 400 }
    )
  );
  expect(await deleteIdentity(identity)).toEqual({
    case: "referenced",
    references: [
      { kind: "PolicyBinding", name: "standing-grant" },
      { kind: "WorkloadCredential", name: "fake-workloads/standing-agent" },
    ],
  });
});

it.each([
  [Response.json({ code: 9, message: "Identity is managed by Flux" }, { status: 400 }), "Identity is managed by Flux"],
  [new Response("not json", { status: 502 }), "HTTP 502: not json"],
])("reports any other failure", async (response, error) => {
  answer(response);
  expect(await deleteIdentity(identity)).toEqual({ case: "failed", error });
});
