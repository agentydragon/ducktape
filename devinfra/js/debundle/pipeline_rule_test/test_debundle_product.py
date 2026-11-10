"""`debundle_product`'s declared targets and launcher arguments, read from a genquery."""

import os
import shlex
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

PACKAGE = "devinfra/js/debundle/pipeline_rule_test/product"


@pytest.fixture(scope="module")
def rules() -> dict[str, ET.Element]:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    path = runfiles.Rlocation(os.environ["PRODUCT_TARGETS"])
    assert path is not None
    root = ET.fromstring(Path(path).read_text())
    return {rule.attrib["name"].split(":", 1)[1]: rule for rule in root.iter("rule")}


def _string(rule: ET.Element, attr: str) -> str | None:
    node = rule.find(f"string[@name='{attr}']")
    return None if node is None else node.attrib["value"]


def _args(rule: ET.Element) -> list[str]:
    node = rule.find("list[@name='args']")
    assert node is not None
    # `debundle_cli` shell-quotes each argument; `bazel run` tokenizes them.
    return shlex.split(" ".join(item.attrib["value"] for item in node.iter("string")))


def _flag(args: list[str], flag: str, arity: int = 1) -> list[str]:
    index = args.index(flag)
    return args[index + 1 : index + 1 + arity]


def test_declared_targets(rules: dict[str, ET.Element]) -> None:
    # `debundle_cli`'s private `__` helpers are its own business.
    families: defaultdict[str, set[str]] = defaultdict(set)
    for name, rule in rules.items():
        generator = _string(rule, "generator_name")
        if generator is not None and "__" not in name:
            families[generator].add(f"{rule.attrib['class']} {name}")
    assert families == {
        "product": {
            "debundle_pipeline product",
            "filegroup product_analysis",
            "filegroup product_graphs",
            "debundle_pipeline product_readable",
            "debundle_pipeline product_passing",
            "debundle_pipeline product_residual_baseline",
            "filegroup product_residual_baseline_analysis",
            "sh_binary product_cli",
            "sh_binary product_cli_worker",
            "sh_binary product_cli_lazy",
        },
        "product_shared_baseline": {
            "debundle_pipeline product_shared_baseline",
            "filegroup product_shared_baseline_analysis",
            "filegroup product_shared_baseline_graphs",
            "debundle_pipeline product_shared_baseline_residual_baseline",
            "filegroup product_shared_baseline_residual_baseline_analysis",
        },
        "product_minimal": {
            "debundle_pipeline product_minimal",
            "filegroup product_minimal_analysis",
            "filegroup product_minimal_graphs",
        },
    }


@pytest.mark.parametrize(
    ("launcher", "graph_chunk"),
    [("product_cli", "main"), ("product_cli_worker", "worker"), ("product_cli_lazy", "main")],
)
def test_launcher_arguments(rules: dict[str, ET.Element], launcher: str, graph_chunk: str) -> None:
    args = _args(rules[launcher])
    assert _flag(args, "--graph-relpath") == [f"reports/tree/{graph_chunk}/dependency_graph.json.gz"]
    assert _flag(args, "--modules-path") == [f"{PACKAGE}/spec/modules"]
    assert _flag(args, "--spec-root") == [f"{PACKAGE}/spec"]
    assert _flag(args, "--source-root", 2) == ["workspace", f"{PACKAGE}/source"]
    # Every launcher reads the gated pipeline's graph export.
    graph = rules[f"{launcher}__graph_output"]
    srcs = graph.find("list[@name='srcs']")
    assert srcs is not None
    assert [label.attrib["value"] for label in srcs.iter("label")] == [f"//{PACKAGE}:product"]
    assert _string(graph, "output_group") == "dependency_graphs"


def _label(rule: ET.Element, attr: str) -> str:
    node = rule.find(f"label[@name='{attr}']")
    assert node is not None
    return node.attrib["value"]


@pytest.mark.parametrize(
    ("pipeline", "vendor_marks"),
    [
        ("product", "spec/sources/vendor/vendor_marks.yaml"),
        ("product_shared_baseline", "vendor_marks.yaml"),
        ("product_shared_baseline_residual_baseline", "spec/residual_baseline/sources/vendor/vendor_marks.yaml"),
    ],
)
def test_spec_location(rules: dict[str, ET.Element], pipeline: str, vendor_marks: str) -> None:
    rule = rules[pipeline]
    spec = "spec/residual_baseline" if pipeline.endswith("_residual_baseline") else "spec"
    assert _label(rule, "tree_config") == f"//{PACKAGE}:{spec}/spec_config.yaml"
    assert _string(rule, "tree_modules") == f"{spec}/modules"
    assert _label(rule, "tree_vendor_marks") == f"//{PACKAGE}:{vendor_marks}"
    assert _label(rule, "tree_source_root") == f"//{PACKAGE}:source"


if __name__ == "__main__":
    pytest_bazel.main()
