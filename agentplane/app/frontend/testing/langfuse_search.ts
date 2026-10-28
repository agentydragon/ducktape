import type { LangfuseSearchSettings } from "../api/schema";

export const LANGFUSE_SEARCH_SETTINGS: LangfuseSearchSettings = {
  base_url: "https://langfuse.test.invalid",
  project_id: "test-project",
  sandbox_namespace_metadata_key: "attributes.agentplane.namespace",
  sandbox_service_account_metadata_key: "attributes.agentplane.service_account",
};
