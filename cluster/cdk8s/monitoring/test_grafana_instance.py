"""Datasource substitution for authored Grafana dashboards."""

import json
import re

from cdk8s import App, Testing as Cdk8sTesting  # Avoid pytest collection of the SDK Testing class.

from cluster.cdk8s.monitoring import grafana_instance


def test_embedded_dashboard_inputs_resolve_to_provisioned_datasource_uids(scratch_app: App) -> None:
    resources = Cdk8sTesting.synth(grafana_instance.chart(scratch_app))
    datasource_uids = {
        doc["spec"]["datasource"]["uid"]
        for doc in resources
        if doc["kind"] == "GrafanaDatasource" and "uid" in doc["spec"]["datasource"]
    }
    for doc in resources:
        if doc["kind"] != "GrafanaDashboard" or "json" not in doc["spec"]:
            continue
        payload = doc["spec"]["json"]
        inputs = set(re.findall(r"\$\{(DS_\w+)\}", payload))
        mappings = {item["inputName"]: item["datasourceName"] for item in doc["spec"].get("datasources", [])}
        assert inputs == mappings.keys(), doc["metadata"]["name"]
        assert set(mappings.values()) <= datasource_uids
        for name, uid in mappings.items():
            payload = payload.replace("${" + name + "}", uid)
        dashboard = json.loads(payload)
        assert dashboard["uid"]
        panels = list(dashboard["panels"])
        while panels:
            panel = panels.pop()
            panels.extend(panel.get("panels", []))
            datasource = panel.get("datasource")
            if not datasource or datasource.get("type") not in {"prometheus", "loki"}:
                continue
            for target in panel.get("targets", []):
                assert target.get("datasource", datasource) == datasource, (
                    dashboard["uid"],
                    panel["id"],
                    target["refId"],
                )
