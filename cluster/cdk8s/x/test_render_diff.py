import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_bazel

from cluster.cdk8s.x.render_diff import (
    Blob,
    BlobStore,
    Failed,
    Graph,
    Renderer,
    Revision,
    apply_copy,
    compare,
    flux_ignore,
    git_tree,
    reconcile,
)

_GOTK_SYNC = """\
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata: {name: flux-system, namespace: flux-system}
spec:
  url: https://github.com/agentydragon/ducktape.git
  sparseCheckout: [cluster/k8s/]
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {name: flux-system, namespace: flux-system}
spec: {path: ./cluster/k8s/flux, sourceRef: {kind: GitRepository, name: flux-system}}
"""
_FLUX = """\
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {name: generators, namespace: flux-system}
spec: {path: ./cluster/k8s/generators, sourceRef: {kind: GitRepository, name: flux-system}}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {name: app, namespace: flux-system}
spec:
  path: ./cluster/k8s/app
  targetNamespace: app
  sourceRef: {kind: ExternalArtifact, name: app}
"""
_GENERATOR = """\
apiVersion: source.extensions.fluxcd.io/v1beta1
kind: ArtifactGenerator
metadata: {name: artifacts, namespace: flux-system}
spec:
  sources: [{alias: repo, kind: GitRepository, name: flux-system, namespace: flux-system}]
  artifacts:
    - name: app
      copy:
        - {from: "@repo/cluster/k8s/app/**", to: "@artifact/cluster/k8s/app/", exclude: ["*.md"]}
        - {from: "@repo/cluster/k8s/base/", to: "@artifact/cluster/k8s/"}
"""


def _config_map(name: str, value: str) -> str:
    return f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: {name}}}\ndata: {{key: {value}}}\n"


def _commit(repo: Path, files: dict[str, str]) -> str:
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.name", "t"), ("user.email", "t@example.test")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    return repo


@pytest.fixture
def base_files() -> dict[str, str]:
    return {
        "cluster/k8s/flux/flux-system/gotk-sync.yaml": _GOTK_SYNC,
        "cluster/k8s/flux/kustomizations.yaml": _FLUX,
        "cluster/k8s/generators/generator.yaml": _GENERATOR,
        # flux/ and generators/ have no kustomization.yaml: kustomize-controller generates one.
        "cluster/k8s/app/config.yaml": _config_map("app", "one"),
        "cluster/k8s/app/README.md": "not copied",
        "cluster/k8s/app/kustomization.yaml": "resources: [config.yaml, ../base]\n",
        "cluster/k8s/base/kustomization.yaml": "resources: [shared.yaml]\n",
        "cluster/k8s/base/shared.yaml": _config_map("shared", "x"),
        # Outside the sparse checkout.
        "other/ignored.yaml": _config_map("outside", "x"),
    }


@pytest.fixture
def render_graph(kustomize: Path, tmp_path: Path) -> Callable[[Path, str], Graph]:
    cache = tmp_path / "cache"
    store = BlobStore(cache / "blobs")
    renderer = Renderer(cache, store, str(kustomize))

    def render(repo: Path, rev: str) -> Graph:
        return reconcile(Revision(repo, rev, git_tree(repo, rev), cache / "upstream", store), renderer, jobs=2)

    return render


def test_identical_revisions_compare_clean(
    render_graph: Callable[[Path, str], Graph], repo: Path, base_files: dict[str, str]
) -> None:
    rev = _commit(repo, base_files)
    graph = render_graph(repo, rev)
    assert set(graph.results) == {("flux-system", n) for n in ("flux-system", "generators", "app")}
    assert compare(graph, render_graph(repo, rev)) == []


def test_reports_changed_field_and_removed_object(
    render_graph: Callable[[Path, str], Graph], repo: Path, base_files: dict[str, str]
) -> None:
    base = _commit(repo, base_files)
    head = _commit(
        repo,
        {
            "cluster/k8s/app/config.yaml": _config_map("app", "two"),
            "cluster/k8s/base/kustomization.yaml": "resources: []\n",
            "cluster/k8s/app/README.md": "changes nothing Flux applies",
        },
    )
    report = compare(render_graph(repo, base), render_graph(repo, head))
    assert report == [
        "== flux-system/app ==",
        '~ ConfigMap app/app\n    data.key: "one" -> "two"',
        "- ConfigMap app/shared",
    ]


def test_build_failure_is_a_difference(
    render_graph: Callable[[Path, str], Graph], repo: Path, base_files: dict[str, str]
) -> None:
    base = _commit(repo, base_files)
    head = _commit(repo, {"cluster/k8s/app/kustomization.yaml": "resources: [missing.yaml]\n"})
    head_graph = render_graph(repo, head)
    assert isinstance(head_graph.results[("flux-system", "app")], Failed)
    assert compare(render_graph(repo, base), head_graph)[0] == "== flux-system/app =="


def test_kustomization_object_change_is_reported_once(
    render_graph: Callable[[Path, str], Graph], repo: Path, base_files: dict[str, str]
) -> None:
    base = _commit(repo, base_files)
    head = _commit(
        repo, {"cluster/k8s/flux/kustomizations.yaml": _FLUX.replace("targetNamespace: app", "targetNamespace: app2")}
    )
    report = compare(render_graph(repo, base), render_graph(repo, head))
    assert report[:2] == [
        "== Flux Kustomization objects ==",
        '~ flux-system/app\n    spec.targetNamespace: "app" -> "app2"',
    ]
    assert "== flux-system/flux-system ==" not in report


def test_source_ignore_reincludes_a_single_file(tmp_path: Path) -> None:
    blob = Blob(Path("/unused"), "0")
    files = dict.fromkeys(
        ("plugin/kubernetes/crd.yaml", "plugin/kubernetes/sample.yaml", "plugin/x.go", "a.yaml"), blob
    )
    ignore = textwrap.dedent(
        """\
        /*
        !/plugin
        /plugin/*
        !/plugin/kubernetes
        /plugin/kubernetes/*
        !/plugin/kubernetes/crd.yaml
        """
    )
    assert set(flux_ignore(files, ignore, BlobStore(tmp_path))) == {"plugin/kubernetes/crd.yaml"}


def test_default_ignore_drops_sops_config(tmp_path: Path) -> None:
    blob = Blob(Path("/unused"), "0")
    files = dict.fromkeys(("cluster/.sops.yaml", "cluster/x.sops.yaml"), blob)
    assert set(flux_ignore(files, None, BlobStore(tmp_path))) == {"cluster/x.sops.yaml"}


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ({"from": "@r/d/**", "to": "@artifact/out/"}, {"out/a.yaml", "out/sub/b.md"}),
        ({"from": "@r/d/", "to": "@artifact/out/"}, {"out/d/a.yaml", "out/d/sub/b.md"}),
        ({"from": "@r/d/a.yaml", "to": "@artifact/out/"}, {"out/a.yaml"}),
        ({"from": "@r/d/a.yaml", "to": "@artifact/renamed.yaml"}, {"renamed.yaml"}),
        ({"from": "@r/d/**", "to": "@artifact/out/", "exclude": ["*.md"]}, {"out/a.yaml"}),
        ({"from": "@r/d/**", "to": "@artifact/out/", "exclude": ["sub/**"]}, {"out/a.yaml"}),
    ],
)
def test_copy_semantics(op: dict[str, object], expected: set[str]) -> None:
    blob = Blob(Path("/unused"), "0")
    artifact: dict[str, Blob] = {}
    apply_copy({"r": {"d/a.yaml": blob, "d/sub/b.md": blob, "e/c.yaml": blob}}, op, artifact)
    assert set(artifact) == expected


if __name__ == "__main__":
    pytest_bazel.main()
