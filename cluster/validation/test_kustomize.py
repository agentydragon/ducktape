from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kustomize import flux_generated_kustomization, stage_artifact_copies


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


if __name__ == "__main__":
    pytest_bazel.main()
