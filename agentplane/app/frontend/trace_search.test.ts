import { expect, it } from "vitest";

import type { TraceSearchSettings } from "./api/schema";
import { traceSearchUrl } from "./trace_search";

const settings: TraceSearchSettings = {
  grafana_base_url: "https://grafana.example.test/observability/",
  tempo_datasource_uid: "tempo-uid",
  deployment_environment: "agentplane-staging",
};

function pane(url: string): Record<string, unknown> {
  return JSON.parse(new URL(url).searchParams.get("panes") ?? "{}") as Record<string, unknown>;
}

it.each([
  ["sandbox", 'sandbox "quoted"\\name', "span.sandbox.name"],
  ["session", "session-123", "span.session.id"],
] as const)("builds a scoped Grafana Explore link for a %s", (kind, id, attribute) => {
  const url = new URL(traceSearchUrl(settings, { kind, id }));
  expect(url.origin).toBe("https://grafana.example.test");
  expect(url.pathname).toBe("/observability/explore");
  expect(url.searchParams.get("schemaVersion")).toBe("1");
  expect(url.searchParams.get("orgId")).toBe("1");

  const explorePane = pane(url.toString()).A as {
    datasource: string;
    queries: { datasource: { type: string; uid: string }; queryType: string; query: string }[];
    range: { from: string; to: string };
  };
  expect(explorePane.datasource).toBe("tempo-uid");
  const expectedQuery =
    kind === "sandbox"
      ? String.raw`{ span.sandbox.name = "sandbox \"quoted\"\\name" && resource.deployment.environment.name = "agentplane-staging" }`
      : String.raw`{ span.session.id = "session-123" && resource.deployment.environment.name = "agentplane-staging" }`;
  expect(explorePane.queries[0]).toEqual({
    refId: "A",
    datasource: { type: "tempo", uid: "tempo-uid" },
    queryType: "traceql",
    query: expectedQuery,
    limit: 20,
  });
  expect(expectedQuery).toContain(attribute);
  expect(explorePane.range).toEqual({ from: "now-24h", to: "now" });
});

it("escapes quotes, backslashes, and newlines in the deployment environment", () => {
  const configured = { ...settings, deployment_environment: 'stage "one"\\west\nnext' };
  const url = new URL(traceSearchUrl(configured, { kind: "session", id: "s" }));
  const explorePane = pane(url.toString()).A as { queries: { query: string }[] };
  expect(explorePane.queries[0].query).toBe(
    String.raw`{ span.session.id = "s" && resource.deployment.environment.name = "stage \"one\"\\west\nnext" }`
  );
});

it("uses the Grafana root when the configured base URL has no path", () => {
  const url = new URL(
    traceSearchUrl({ ...settings, grafana_base_url: "https://grafana.example.test" }, { kind: "session", id: "s" })
  );
  expect(url.pathname).toBe("/explore");
});
