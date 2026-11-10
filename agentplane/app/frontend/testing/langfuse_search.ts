import { create } from "@bufbuild/protobuf";

import { LangfuseSearchSettingsSchema, type LangfuseSearchSettings } from "../../app_service_pb";

export const LANGFUSE_SEARCH_SETTINGS: LangfuseSearchSettings = create(LangfuseSearchSettingsSchema, {
  baseUrl: "https://langfuse.test.invalid",
  projectId: "test-project",
  sessionMetadataKey: "attributes.litellm.metadata.spend_logs_metadata.agentplane.session_id",
  sandboxNamespaceMetadataKey: "attributes.agentplane.namespace",
  sandboxServiceAccountMetadataKey: "attributes.agentplane.service_account",
});
