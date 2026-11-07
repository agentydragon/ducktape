import type { LangfuseSearchSettings } from "../app_config_client";

export const LANGFUSE_SEARCH_SETTINGS: LangfuseSearchSettings = {
  base_url: "https://langfuse.test.invalid",
  project_id: "test-project",
  session_metadata_key: "attributes.litellm.metadata.spend_logs_metadata.agentplane.session_id",
  sandbox_namespace_metadata_key: "attributes.agentplane.namespace",
  sandbox_service_account_metadata_key: "attributes.agentplane.service_account",
};
