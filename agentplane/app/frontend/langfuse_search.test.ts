import { expect, it } from "vitest";

import type { LangfuseSearchSettings } from "../app_service_pb";
import { langfuseSandboxTraceUrl, langfuseSessionTraceUrl } from "./langfuse_search";
import { LANGFUSE_SEARCH_SETTINGS } from "./testing/langfuse_search";

const settings: LangfuseSearchSettings = {
  ...LANGFUSE_SEARCH_SETTINGS,
  baseUrl: "https://langfuse.example.test/observability/",
  projectId: "langfuse-litellm-project",
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
    projectId: "project;one,west",
    sandboxNamespaceMetadataKey: "metadata.agentplane;untrusted,namespace",
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
      { ...settings, baseUrl: "https://langfuse.example.test" },
      {
        namespace: "agentplane-staging",
        serviceAccount: "ready-sandbox",
      }
    )
  );
  expect(url.pathname).toBe("/project/langfuse-litellm-project/traces");
});

it("scopes central sampling traces to the public Session independently of its execution Sandbox", () => {
  const url = new URL(
    langfuseSessionTraceUrl({ ...settings, sessionMetadataKey: "attributes.agentplane.session_id" }, "central-session")
  );
  expect(url.searchParams.get("filter")).toBe(
    "metadata;stringObject;attributes.agentplane.session_id;=;central-session"
  );
});

it("uses the metadata attribute promoted from trusted ingress attribution by default", () => {
  const url = new URL(langfuseSessionTraceUrl(settings, "central-session"));
  expect(url.searchParams.get("filter")).toBe(
    "metadata;stringObject;attributes.litellm.metadata.spend_logs_metadata.agentplane.session_id;=;central-session"
  );
});
