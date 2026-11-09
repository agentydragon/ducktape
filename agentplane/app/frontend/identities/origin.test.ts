import { create } from "@bufbuild/protobuf";
import { expect, it } from "vitest";

import { IdentitySchema } from "../../../policy/v1/policy_service_pb";
import { identityOrigin } from "./origin";

it.each([
  [{ "kustomize.toolkit.fluxcd.io/name": "fake-kustomization" }, "git"],
  [{ "app.kubernetes.io/managed-by": "fake-tool" }, "unknown"],
  [{}, "unknown"],
])("derives origin %j as %s", (labels, origin) => {
  expect(identityOrigin(create(IdentitySchema, { labels }))).toBe(origin);
});
