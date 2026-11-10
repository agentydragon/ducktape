import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.validation.kustomize import flux_generated_kustomization, stage_artifact_copies


@pytest.fixture
def image_pins(runfiles: Runfiles, tmp_path: Path) -> dict[str, Any]:
    source = runfiles.root() / "ducktape/cluster/k8s/agentplane-staging/image-pins"
    shutil.copytree(source, tmp_path / "image-pins")
    return cast(dict[str, Any], yaml.safe_load((tmp_path / "image-pins/kustomization.yaml").read_text()))


@pytest.fixture(scope="module")
def kustomize(runfiles: Runfiles) -> Path:
    return runfiles.root() / "multitool/tools/kustomize/kustomize"


def test_flux_generated_kustomization_scans_like_kustomize_controller(tmp_path: Path) -> None:
    for rel in ["app.k8s.yaml", "notes.md", "config.json", "plain/secret.sops.yaml", "overlay/kustomization.yaml"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("")

    kust = flux_generated_kustomization(tmp_path)

    # A subdirectory with its own kustomization is one resource; any other YAML file,
    # nested or not, is its own resource; non-YAML files are not resources.
    assert set(kust.resources) == {Path("app.k8s.yaml"), Path("overlay"), Path("plain/secret.sops.yaml")}


def test_stage_artifact_copies_directory_and_exact_file_without_changing_source(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    artifact_root = tmp_path / "artifact"
    base = repo_root / "cluster/generated/ai-session-archive"
    base.mkdir(parents=True)
    (base / "kustomization.yaml").write_text("kind: Kustomization\n")
    encrypted_source = repo_root / "secrets/shared/session-backup.yaml"
    encrypted_source.parent.mkdir(parents=True)
    encrypted_source.write_text("ENC[synthetic-ciphertext]\n")

    stage_artifact_copies(
        repo_root,
        artifact_root,
        [
            ("@repo/cluster/generated/ai-session-archive/**", "@artifact/cluster/generated/ai-session-archive/"),
            (
                "@repo/secrets/shared/session-backup.yaml",
                "@artifact/cluster/generated/ai-session-archive/session-backup.sops.yaml",
            ),
        ],
    )

    assert (artifact_root / "cluster/generated/ai-session-archive/kustomization.yaml").is_file()
    assert (
        artifact_root / "cluster/generated/ai-session-archive/session-backup.sops.yaml"
    ).read_text() == "ENC[synthetic-ciphertext]\n"
    assert not (repo_root / "cluster/generated/ai-session-archive/session-backup.sops.yaml").exists()


def test_stage_artifact_copies_reject_unsafe_or_missing_paths(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(ValueError, match="safe relative path"):
        stage_artifact_copies(repo_root, tmp_path / "artifact", [("@repo/../secret", "@artifact/a")])
    with pytest.raises(ValueError, match="safe relative path"):
        stage_artifact_copies(repo_root, tmp_path / "artifact", [("@repo/source.yaml", "@artifact/../escape")])
    with pytest.raises(FileNotFoundError, match="source is missing"):
        stage_artifact_copies(repo_root, tmp_path / "artifact", [("@repo/missing.yaml", "@artifact/a.yaml")])


def test_environment_tools_image_pin_reaches_every_sandbox_template_container(
    image_pins: dict[str, Any], kustomize: Path, tmp_path: Path
) -> None:
    environment_tools_image = "git.allegedly.works/ducktape-ci/environment-tools"
    environment_tools_pin = next(image for image in image_pins["images"] if image["name"] == environment_tools_image)
    (tmp_path / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources: [sandbox-template.yaml]\n"
        "components: [image-pins]\n"
    )
    (tmp_path / "sandbox-template.yaml").write_text(
        "apiVersion: agentplane.allegedly.works/v1alpha1\n"
        "kind: SandboxTemplate\n"
        "metadata:\n"
        "  name: environment-tools\n"
        "spec:\n"
        "  pod:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: environment-runtime\n"
        f"          image: {environment_tools_image}:unset\n"
        "        - name: codex-executor\n"
        f"          image: {environment_tools_image}:unset\n"
    )
    result = subprocess.run([kustomize, "build", tmp_path], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()
    [template] = yaml.safe_load_all(result.stdout)
    images = [container["image"] for container in template["spec"]["pod"]["spec"]["containers"]]
    assert images == [f"{environment_tools_image}:{environment_tools_pin['newTag']}"] * 2


def test_egress_sidecar_image_pin_reaches_launcher_relay_template(
    image_pins: dict[str, Any], kustomize: Path, tmp_path: Path
) -> None:
    sidecar_image = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar"
    sidecar_pin = next(image for image in image_pins["images"] if image["name"] == sidecar_image)
    (tmp_path / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources: [relay-policy.yaml]\n"
        "components: [image-pins]\n"
    )
    (tmp_path / "relay-policy.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "kyverno.io/v1",
                "kind": "ClusterPolicy",
                "metadata": {"name": "test-launcher-relay"},
                "spec": {
                    "rules": [
                        {
                            "name": "inject-test-relay",
                            "context": [{"name": "relay", "variable": {"value": {"image": f"{sidecar_image}:unset"}}}],
                        }
                    ]
                },
            }
        )
    )
    result = subprocess.run([kustomize, "build", tmp_path], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()
    [policy] = yaml.safe_load_all(result.stdout)
    relay = policy["spec"]["rules"][0]["context"][0]["variable"]["value"]
    assert relay["image"] == f"{sidecar_image}:{sidecar_pin['newTag']}"


if __name__ == "__main__":
    pytest_bazel.main()
