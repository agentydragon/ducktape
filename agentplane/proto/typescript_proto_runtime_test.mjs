import assert from "node:assert/strict";
import { getOption } from "@bufbuild/protobuf";
import { ActionService } from "../../agentplane_proto_generated/agentplane/action_service/action_service_pb.js";
import { http } from "../../agentplane_proto_generated/google/api/annotations_pb.js";

const submitActionRequest = getOption(ActionService.method.submitActionRequest, http);
assert.equal(submitActionRequest.pattern.case, "post");
assert.equal(submitActionRequest.pattern.value, "/v1/action-requests");
assert.equal(submitActionRequest.body, "*");
