import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { create, getOption } from "@bufbuild/protobuf";
import { ActionService } from "../../agentplane_proto_generated/agentplane/action_service/action_service_pb.js";
import { http } from "../../agentplane_proto_generated/google/api/annotations_pb.js";
import { RootSchema } from "./typescript_transitive_fixture_generated/agentplane/proto/typescript_generation_test/root_pb.js";

const submitActionRequest = getOption(ActionService.method.submitActionRequest, http);
assert.equal(submitActionRequest.pattern.case, "post");
assert.equal(submitActionRequest.pattern.value, "/v1/action-requests");
assert.equal(submitActionRequest.body, "*");

const root = create(RootSchema, {
  left: { shared: { value: "left" } },
  right: { shared: { value: "right" } },
  createdAt: { seconds: 123n, nanos: 0 },
});
assert.equal(root.left.shared.value, "left");
assert.equal(root.right.shared.value, "right");
assert.equal(root.createdAt.seconds, 123n);
assert.equal(
  existsSync(new URL("./typescript_transitive_fixture_generated/fixture/shared_pb.js", import.meta.url)),
  true
);

for (const wkt of ["descriptor", "duration", "empty", "struct", "timestamp"]) {
  assert.equal(
    existsSync(new URL(`../../agentplane_proto_generated/google/protobuf/${wkt}_pb.js`, import.meta.url)),
    false
  );
}
