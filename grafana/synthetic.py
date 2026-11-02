"""Deterministic synthetic datasource responses for Grafana visual tests."""

from __future__ import annotations

import asyncio
import json
import re

from playwright.async_api import Route

from util.testing.frontend_visual import FROZEN_NOW_MS
from util.testing.undeclared_outputs import undeclared_outputs_dir


async def synthetic_query(route: Route, seen: asyncio.Event, fixture: dict, log_panels: set[str]) -> None:
    payload = route.request.post_data_json
    assert payload is not None
    with (undeclared_outputs_dir() / "synthetic-queries.jsonl").open("a") as log:
        log.write(json.dumps(payload) + "\n")
    start = FROZEN_NOW_MS - 24 * 60 * 60 * 1000
    times = [start + i * 60 * 60 * 1000 for i in range(25)]
    results = {}
    for query in payload["queries"]:
        ref = query["refId"]
        request_id = query.get("requestId", "")
        panel_id = request_id.removesuffix(ref)
        panel = fixture.get("panels", {}).get(panel_id, {})
        label_context = query.get("expr", "") + " " + query.get("legendFormat", "")
        labels = fixture.get("labels", {})
        selected = {
            key: values for key, values in labels.items() if re.search(r"\b" + re.escape(key) + r"\b", label_context)
        }
        if panel_id in log_panels:
            results[ref] = {
                "refId": ref,
                "frames": [
                    {
                        "schema": {
                            "refId": ref,
                            "fields": [
                                {"name": "Time", "type": "time", "typeInfo": {"frame": "time.Time"}},
                                {"name": "Line", "type": "string"},
                            ],
                        },
                        "data": {
                            "values": [
                                [FROZEN_NOW_MS - 120000, FROZEN_NOW_MS - 60000],
                                ["synthetic: service became ready", "synthetic: state changed"],
                            ]
                        },
                    }
                ],
            }
            continue
        if "table" in panel:
            columns = panel["table"]
            results[ref] = {
                "refId": ref,
                "frames": [
                    {
                        "schema": {
                            "refId": ref,
                            "fields": [{"name": col["name"], "type": col["type"]} for col in columns],
                        },
                        "data": {"values": [col["values"] for col in columns]},
                    }
                ],
            }
            continue
        if query.get("format") == "table":
            # Prometheus returns one instant-value frame per label set; its
            # frontend converts those frames to table rows before joins.
            frames = [
                {
                    "schema": {
                        "refId": ref,
                        "meta": {"type": "numeric-multi", "typeVersion": [0, 1], "custom": {"resultType": "vector"}},
                        "fields": [
                            {"name": "Time", "type": "time", "typeInfo": {"frame": "time.Time"}},
                            {
                                "name": "Value",
                                "type": "number",
                                "typeInfo": {"frame": "float64"},
                                "labels": {key: values[index] for key, values in selected.items()},
                            },
                        ],
                    },
                    "data": {"values": [[FROZEN_NOW_MS], [25 + 15 * index]]},
                }
                for index in range(2)
            ]
            results[ref] = {"refId": ref, "frames": frames}
            continue
        frames = []
        for index, series in enumerate(panel.get("series", ["example-a", "example-b"])):
            series_labels = {"series": series}
            if "series" not in panel:
                series_labels.update({key: values[index] for key, values in selected.items()})
            frames.append(
                {
                    "schema": {
                        "refId": ref,
                        "fields": [
                            {"name": "Time", "type": "time", "typeInfo": {"frame": "time.Time"}},
                            {"name": "Value", "type": "number", "labels": series_labels},
                        ],
                    },
                    "data": {"values": [times, [20 + index * 15 + (i % 7) * 2 for i in range(25)]]},
                }
            )
        results[ref] = {"frames": frames, "refId": ref}
    await route.fulfill(json={"results": results})
    seen.set()
