import json
import sys

import pytest
import pytest_bazel

from devinfra.ci.skills.ci_latency.scripts import attribution, publish


def test_shared_setup_and_overlapping_triggers():
    result = attribution.calculate(
        {
            "resource": "runner-seconds",
            "runs": [
                {
                    "id": "run-1",
                    "source_commit": "sha",
                    "units": [
                        {"name": "provision", "kind": "provision", "seconds": 90, "triggers": ["a", "b"]},
                        {"name": "test", "kind": "test", "seconds": 30, "triggers": ["b", "c"]},
                    ],
                }
            ],
        }
    )
    assert result["measured_seconds"] == 120
    assert {x["trigger"]: x["seconds"] for x in result["attribution"]} == {"a": 45, "b": 60, "c": 15}


@pytest.mark.parametrize(("triggers", "seconds"), [([], 3), (["a", "a"], 3), (["a"], -1), (["a"], float("nan"))])
def test_bad_values(triggers, seconds):
    with pytest.raises(ValueError, match=r"unit seconds|unit needs|duplicate trigger"):
        attribution.calculate(
            {
                "resource": "worker-seconds",
                "runs": [
                    {
                        "id": "x",
                        "source_commit": "sha",
                        "units": [{"name": "u", "kind": "test", "seconds": seconds, "triggers": triggers}],
                    }
                ],
            }
        )


def test_publish_updates_only_selected_namespace_and_removes_obsolete_artifacts(tmp_path, monkeypatch):
    history = tmp_path / "history"
    history.mkdir()
    (history / "README.md").write_text("Navigation stays at the root.\n")
    (history / "unrelated.txt").write_text("Keep this file.\n")
    names = ["report.md", "evidence.json", "manifest.json", "index.html", "attribution.json"]
    for kind in ["latency", "reliability"]:
        directory = history / kind
        directory.mkdir()
        for name in names:
            (directory / name).write_text(f"old {kind} snapshot\n")

    report = tmp_path / "report.html"
    report.write_text(
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8"><title>CI latency</title></head>\n'
        '<body><main><h1>CI latency</h1><p>Read the <a href="https://example.test/run">run</a>.</p>\n'
        '<table><caption>Queue time, seconds (n=3)</caption><thead><tr><th scope="col">Class</th>'
        '<th scope="col">Median</th></tr></thead><tbody><tr><th scope="row">Python</th>'
        "<td>12</td></tr></tbody></table></main></body></html>\n"
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"jobs": 3}\n')
    monkeypatch.setattr(publish, "git", lambda *_: "commit")
    sibling = history / "reliability"
    sibling_before = {path.name: path.read_bytes() for path in sibling.iterdir()}

    def publish_kind(kind):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "publish.py",
                "--source",
                "a" * 40,
                "--window-start",
                "2026-10-04T20:00:00+00:00",
                "--window-end",
                "2026-10-04T21:00:00+00:00",
                "--kind",
                kind,
                "--report",
                str(report),
                "--evidence",
                str(evidence),
                "--out",
                str(history),
            ],
        )
        publish.main()

    publish_kind("latency")

    latency = history / "latency"
    assert not (latency / "report.md").exists()
    assert json.loads((latency / "evidence.json").read_text()) == {"jobs": 3}
    manifest = json.loads((latency / "manifest.json").read_text())
    assert manifest["source_devel_commit"] == "a" * 40
    assert manifest["window_start_utc"] == "2026-10-04T20:00:00+00:00"
    assert manifest["window_end_utc"] == "2026-10-04T21:00:00+00:00"
    assert manifest["attribution"] == "not collected"
    assert (latency / "index.html").read_bytes() == report.read_bytes()
    assert not (latency / "attribution.json").exists()
    assert {path.name: path.read_bytes() for path in sibling.iterdir()} == sibling_before

    latency_after_publish = {path.name: path.read_bytes() for path in latency.iterdir()}
    publish_kind("reliability")

    reliability = history / "reliability"
    assert not (reliability / "report.md").exists()
    assert json.loads((reliability / "evidence.json").read_text()) == {"jobs": 3}
    assert json.loads((reliability / "manifest.json").read_text())["attribution"] == ("not collected")
    assert (reliability / "index.html").read_bytes() == report.read_bytes()
    assert not (reliability / "attribution.json").exists()
    assert {path.name: path.read_bytes() for path in latency.iterdir()} == latency_after_publish
    assert (history / "README.md").read_text() == "Navigation stays at the root.\n"
    assert (history / "unrelated.txt").read_text() == "Keep this file.\n"
    assert {path.name for path in history.iterdir()} == {"README.md", "unrelated.txt", "latency", "reliability"}


if __name__ == "__main__":
    pytest_bazel.main()
