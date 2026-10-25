import { useEffect, useState } from "react";

import { api, type TraceSearchSettings } from "./client";

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

/** Load the app-owned optional Grafana target once per page. An absent or unavailable setting hides links. */
export function useTraceSearchSettings(): TraceSearchSettings | null {
  const [settings, setSettings] = useState<TraceSearchSettings | null>(null);
  useEffect(() => {
    let current = true;
    void api
      .GET("/trace-search")
      .then(({ data, error }) => {
        if (current) setSettings(error === undefined ? (data ?? null) : null);
      })
      .catch(() => {
        if (current) setSettings(null);
      });
    return () => {
      current = false;
    };
  }, []);
  return settings;
}
