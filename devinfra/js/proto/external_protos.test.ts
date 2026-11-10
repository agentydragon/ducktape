import { create } from "@bufbuild/protobuf";
import { expect, it } from "vitest";

import { SessionManagement } from "../../../platform_sessions/management/v1/management_pb";
import { ArtifactSchema } from "../../buildbuddy/bbapi/proto/bbapi_pb";
import { ExternalInvocationSchema } from "./external_invocation_fixture_pb";

it("loads external Google and BuildBuddy proto dependencies", () => {
  expect(SessionManagement.typeName).toBe("platform_sessions.management.v1.SessionManagement");
  expect(create(ArtifactSchema, { kind: "artifact" }).kind).toBe("artifact");
  expect(ExternalInvocationSchema.typeName).toBe("ducktape.proto_test.ExternalInvocation");
});
