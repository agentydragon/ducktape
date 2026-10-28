import type { LangfuseSearchSettings } from "./schema";

export type LangfuseSandbox = { namespace: string; serviceAccount: string };

function metadataFilter(key: string, value: string): string {
  // Langfuse's filter grammar separates fields with semicolons and filters with commas. Encode both
  // configurable keys and values before URLSearchParams applies the outer query-string encoding.
  return `metadata;stringObject;${encodeURIComponent(key)};=;${encodeURIComponent(value)}`;
}

/** Build a Langfuse v4 trace-list URL filtered to the Sandbox's attributed namespace and account. */
export function langfuseSandboxTraceUrl(settings: LangfuseSearchSettings, sandbox: LangfuseSandbox): string {
  const base = settings.base_url.endsWith("/") ? settings.base_url : `${settings.base_url}/`;
  const url = new URL(`project/${encodeURIComponent(settings.project_id)}/traces`, base);
  // Langfuse v4.67.0's TIME_RANGES.last1Day abbreviation is `1d`.
  url.searchParams.set("dateRange", "1d");
  url.searchParams.set(
    "filter",
    [
      metadataFilter(settings.sandbox_namespace_metadata_key, sandbox.namespace),
      metadataFilter(settings.sandbox_service_account_metadata_key, sandbox.serviceAccount),
    ].join(",")
  );
  return url.toString();
}
