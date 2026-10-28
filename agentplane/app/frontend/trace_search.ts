import type { TraceSearchSettings } from "./api/schema";

type TraceTarget = { kind: "sandbox" | "session"; id: string };

/** Make an Explore link whose TraceQL literal and URL query are independently escaped. */
export function traceSearchUrl(settings: TraceSearchSettings, target: TraceTarget): string {
  const attribute = target.kind === "sandbox" ? "span.sandbox.name" : "span.session.id";
  const query = `{ ${attribute} = ${JSON.stringify(target.id)} && resource.deployment.environment.name = ${JSON.stringify(settings.deployment_environment)} }`;
  const base = settings.grafana_base_url.endsWith("/") ? settings.grafana_base_url : `${settings.grafana_base_url}/`;
  const url = new URL("explore", base);
  url.searchParams.set("schemaVersion", "1");
  url.searchParams.set("orgId", "1");
  url.searchParams.set(
    "panes",
    JSON.stringify({
      A: {
        datasource: settings.tempo_datasource_uid,
        queries: [
          {
            refId: "A",
            datasource: { type: "tempo", uid: settings.tempo_datasource_uid },
            queryType: "traceql",
            query,
            limit: 20,
          },
        ],
        range: { from: "now-24h", to: "now" },
      },
    })
  );
  return url.toString();
}
