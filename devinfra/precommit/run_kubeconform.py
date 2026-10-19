"""Validate manifests using the Seaweed schema derived from its pinned upstream CRD."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml


def main() -> None:
    # MODULE.bazel's literal http_file declaration owns both the URL and digest.
    # Read it as syntax; never evaluate repository configuration as Python code.
    declarations = (
        node.value
        for node in ast.parse(Path("MODULE.bazel").read_text()).body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "http_file"
    )
    (declaration,) = [
        call
        for call in declarations
        if any(
            kw.arg == "name" and isinstance(kw.value, ast.Constant) and kw.value.value == "seaweed_seaweed_crd"
            for kw in call.keywords
        )
    ]
    pin = {kw.arg: ast.literal_eval(kw.value) for kw in declaration.keywords if kw.arg in {"urls", "sha256"}}
    with urllib.request.urlopen(pin["urls"][0], timeout=60) as response:
        source = response.read()
    if hashlib.sha256(source).hexdigest() != pin["sha256"]:
        raise ValueError("Seaweed CRD does not match its MODULE.bazel SHA256")
    crd = yaml.safe_load(source)
    with tempfile.TemporaryDirectory(prefix="kubeconform-schemas-") as directory:
        root = Path(directory)
        group = root / crd["spec"]["group"]
        group.mkdir()
        kind = crd["spec"]["names"]["kind"].lower()
        for version in crd["spec"]["versions"]:
            if version["served"]:
                (group / f"{kind}_{version['name']}.json").write_text(json.dumps(version["schema"]["openAPIV3Schema"]))
        result = subprocess.run(
            [
                "kubeconform",
                "-schema-location",
                "default",
                "-schema-location",
                f"{root}/{{{{.Group}}}}/{{{{.ResourceKind}}}}_{{{{.ResourceAPIVersion}}}}.json",
                "-schema-location",
                "cluster/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
                "-schema-location",
                "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
                "-ignore-missing-schemas",
                *sys.argv[1:],
            ],
            check=False,
        )
        # Preserve kubeconform's exit status as the hook's own result.
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
