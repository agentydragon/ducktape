import { expect, it } from "vitest";

import { langfuseSandboxTraceUrl, type LangfuseSearchSettings } from "./langfuse_search";
import { LANGFUSE_SEARCH_SETTINGS } from "./testing/langfuse_search";

const settings: LangfuseSearchSettings = {
  ...LANGFUSE_SEARCH_SETTINGS,
  base_url: "https://langfuse.example.test/observability/",
  project_id: "langfuse-litellm-project",
};

it("builds Langfuse's metadata-filter URL for the Sandbox namespace and ServiceAccount", () => {
  const url = new URL(
    langfuseSandboxTraceUrl(settings, { namespace: "agentplane-staging", serviceAccount: "ready-sandbox" })
  );
  expect(url.origin).toBe("https://langfuse.example.test");
  expect(url.pathname).toBe("/observability/project/langfuse-litellm-project/traces");
  expect(url.searchParams.get("dateRange")).toBe("1d");
  expect(url.searchParams.get("filter")).toBe(
    "metadata;stringObject;attributes.agentplane.namespace;=;agentplane-staging," +
      "metadata;stringObject;attributes.agentplane.service_account;=;ready-sandbox"
  );
});

it("escapes configured filter keys and metadata values before the outer URL encoding", () => {
  const adversarial: LangfuseSearchSettings = {
    ...settings,
    project_id: "project;one,west",
    sandbox_namespace_metadata_key: "metadata.agentplane;untrusted,namespace",
  };
  const url = new URL(
    langfuseSandboxTraceUrl(adversarial, {
      namespace: "agentplane west;qa,blue%",
      serviceAccount: 'sandbox "quoted"',
    })
  );
  expect(url.pathname).toBe("/observability/project/project%3Bone%2Cwest/traces");
  expect(url.searchParams.get("dateRange")).toBe("1d");
  expect(url.searchParams.get("filter")).toBe(
    "metadata;stringObject;metadata.agentplane%3Buntrusted%2Cnamespace;=;agentplane%20west%3Bqa%2Cblue%25," +
      "metadata;stringObject;attributes.agentplane.service_account;=;sandbox%20%22quoted%22"
  );
});

it("uses the Langfuse origin when the configured base URL has no path", () => {
  const url = new URL(
    langfuseSandboxTraceUrl(
      { ...settings, base_url: "https://langfuse.example.test" },
      {
        namespace: "agentplane-staging",
        serviceAccount: "ready-sandbox",
      }
    )
  );
  expect(url.pathname).toBe("/project/langfuse-litellm-project/traces");
});
