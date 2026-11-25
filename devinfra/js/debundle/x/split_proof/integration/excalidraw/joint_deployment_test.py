"""One disk-checked graph rooted at the original main.js URL."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def runfile(rf: Runfiles, name: str) -> Path:
    path = rf.Rlocation(name)
    assert path is not None, name
    return Path(path)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select(canonical: dict, chunk: str, owner: str, name: str) -> dict:
    assert canonical["chunk_id"] == chunk
    selected = [
        unit for unit in canonical["units"] if unit["owner"] == owner and name in unit.get("members", {}).values()
    ]
    (one,) = selected
    assert list(one["members"].values()) == [name]
    return {
        "chunk_id": chunk,
        "source_sha256": canonical["source_sha256"],
        "import_order": [owner],
        "units": [one if row == one else {"ordinal": row["ordinal"], "owner": None} for row in canonical["units"]],
    }


def test_joint_deployment(tmp_path: Path) -> None:
    rf = Runfiles.Create()
    assert rf is not None
    bundle = runfile(rf, next(p for p in os.environ["BUNDLE"].split() if p.endswith("/bundle")))
    pins = json.loads(runfile(rf, os.environ["PINS"]).read_text())
    artifacts = undeclared_outputs_dir()
    chunks = {}
    for chunk, owner, binding in [
        ("main", "packages/excalidraw/renderer/interactivescene", "strokeEllipseWithRotation"),
        ("editor", "packages/excalidraw/scene/scroll", "centerScrollOn"),
    ]:
        placement = select(json.loads(runfile(rf, os.environ[chunk.upper()]).read_text()), chunk, owner, binding)
        (path,) = (path for path, sha in pins.items() if sha == placement["source_sha256"])
        assert digest(bundle / path) == placement["source_sha256"]
        chunks[chunk] = path
        (artifacts / f"{chunk}.json").write_text(json.dumps(placement))
    assert chunks["main"] == "main.js"
    origin = "https://excalidraw.test"
    work = tmp_path / "work"
    run = subprocess.run(
        [
            runfile(rf, os.environ["TRIAL"]),
            bundle,
            chunks["main"],
            chunks["editor"],
            artifacts / "main.json",
            artifacts / "editor.json",
            origin,
            work,
        ],
        env={**os.environ, "SPLIT_PROOF_MODULE_IO": str(runfile(rf, os.environ["FRONTEND"]))},
        text=True,
        capture_output=True,
        check=False,
    )
    (artifacts / "proof.log").write_text(run.stdout + run.stderr)
    assert (work / "inventory.json").is_file(), run.stdout + run.stderr
    inventory = json.loads((work / "inventory.json").read_text())
    assert inventory["root"] == chunks["main"]
    assert set(inventory["linked"]) == set(pins)
    # The same provider imported from multiple referrers has one unit range.
    first = 0
    for row in sorted(inventory["linked"].values(), key=lambda row: row["firstUnit"]):
        assert row["firstUnit"] == first
        assert row["unitCount"] > 0
        first += row["unitCount"]
    for name in chunks.values():
        assert inventory["linked"][name]["metadataUrl"] == f"{origin}/{name}"
        assert inventory["linked"][name]["unitCapabilities"]

    resources = inventory["resources"]
    for path, url in list(resources.items()):
        assert url == f"{origin}/{path}"
        sha = digest(work / "snapshot" / path)
        assert digest(bundle / path) == sha
        resources[path] = {"effectiveUrl": url, "sha256": sha}
    assert {p: resources[p]["sha256"] for p in pins} == pins
    stage = work / "staged"
    inventory["staged"] = {p.relative_to(stage).as_posix(): digest(p) for p in stage.rglob("*") if p.is_file()}
    generated = {"packages/excalidraw/renderer/interactivescene.mjs", "packages/excalidraw/scene/scroll.mjs"}
    assert set(inventory["staged"]) == set(pins) | generated
    for path in set(pins) - set(chunks.values()):
        assert inventory["staged"][path] == pins[path]
    inventory["premises"] = {
        "profile": "browser-page",
        "deploymentPreset": "native-browser-record@1",
        "importMap": "empty@1",
    }
    print(
        f"App-root deployment: {len(pins)} linked modules, {len(resources)} input resources, "
        f"{len(inventory['staged'])} staged modules; {first} source ordinals."
    )
    (artifacts / "inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True))
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() == "certified joint normal completion"


if __name__ == "__main__":
    pytest_bazel.main()
