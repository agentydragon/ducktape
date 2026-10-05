import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from devinfra.ci import runner_image

TOOLS_PATH = "/nix/store/aaaaaaaa-runner-tools"
BASE_DIGEST = "sha256:" + "b" * 64
RUNNER_DIGEST = "sha256:" + "c" * 64
EXPECTED_KEY = "d" * 64


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for relative in runner_image.RECIPE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"recipe: {relative}\n", encoding="utf-8")
    (tmp_path / "devinfra/image_pins.json").write_text(
        json.dumps({"rbe_container": {"image": "ghcr.io/example/rbe", "digest": BASE_DIGEST}}), encoding="utf-8"
    )
    (tmp_path / "devinfra/bbr.json").write_text(
        json.dumps({"container_image": f"ghcr.io/example/runner@{RUNNER_DIGEST}"}), encoding="utf-8"
    )
    return tmp_path


def mock_nix(monkeypatch: pytest.MonkeyPatch, tools_path: str = TOOLS_PATH) -> None:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args == ["nix", "eval", "--raw", runner_image.TOOLS_ATTR]
        return subprocess.CompletedProcess(args, 0, stdout=tools_path + "\n", stderr="")

    monkeypatch.setattr(runner_image.subprocess, "run", run)


def test_identity_uses_the_evaluated_closure_and_ignores_unrelated_nix_and_workload_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_nix(monkeypatch)
    before = runner_image.load_inputs(repo).identity

    (repo / "nix/artifact-pins.json").write_text('{"unrelated": "new pin"}', encoding="utf-8")
    (repo / "flake.lock").write_text('{"nodes": "unrelated update"}', encoding="utf-8")
    (repo / "cluster/workloads/example.yaml").parent.mkdir(parents=True)
    (repo / "cluster/workloads/example.yaml").write_text("replicas: 3\n", encoding="utf-8")

    assert runner_image.load_inputs(repo).identity == before


def test_identity_changes_when_the_evaluated_runner_tools_closure_changes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_nix(monkeypatch, TOOLS_PATH)
    first = runner_image.load_inputs(repo).identity
    mock_nix(monkeypatch, "/nix/store/eeeeeeee-runner-tools")
    assert runner_image.load_inputs(repo).identity != first


def test_identity_changes_when_the_pinned_rbe_base_changes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_nix(monkeypatch)
    first = runner_image.load_inputs(repo).identity
    pins = json.loads((repo / "devinfra/image_pins.json").read_text(encoding="utf-8"))
    pins["rbe_container"]["digest"] = "sha256:" + "f" * 64
    (repo / "devinfra/image_pins.json").write_text(json.dumps(pins), encoding="utf-8")
    assert runner_image.load_inputs(repo).identity != first


@pytest.mark.parametrize("relative_path", runner_image.RECIPE_FILES)
def test_identity_changes_when_any_explicit_recipe_file_changes(
    repo: Path, monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> None:
    mock_nix(monkeypatch)
    first = runner_image.load_inputs(repo).identity
    path = repo / relative_path
    path.write_text(path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    assert runner_image.load_inputs(repo).identity != first


def test_identity_inputs_record_platform_and_full_base_ref(repo: Path) -> None:
    inputs = runner_image.compute_inputs(repo, TOOLS_PATH)
    assert inputs.platform == "linux/amd64"
    assert inputs.base_ref == f"ghcr.io/example/rbe@{BASE_DIGEST}"
    assert len(inputs.identity) == 64


def test_missing_label_builds_and_equal_label_reuses(repo: Path) -> None:
    inputs = runner_image.compute_inputs(repo, TOOLS_PATH)
    assert runner_image.needs_build(inputs, None)
    assert not runner_image.needs_build(inputs, {runner_image.INPUTS_LABEL: inputs.identity})
    assert runner_image.needs_build(inputs, {runner_image.INPUTS_LABEL: EXPECTED_KEY})


def test_platform_map_reads_only_the_linux_amd64_config() -> None:
    labels = {runner_image.INPUTS_LABEL: EXPECTED_KEY}
    image = {
        "linux/arm64": {"config": {"Labels": {runner_image.INPUTS_LABEL: "wrong"}}},
        "linux/amd64": {"config": {"Labels": labels}},
    }
    assert runner_image.labels_for_platform(image) == labels


def test_single_platform_config_must_prove_linux_amd64() -> None:
    with pytest.raises(ValueError, match="expected 'linux/amd64'"):
        runner_image.labels_for_platform({"os": "linux", "architecture": "arm64", "config": {}})
    with pytest.raises(ValueError, match="missing its OS or architecture"):
        runner_image.labels_for_platform({"config": {"Labels": {}}})


def test_missing_amd64_entry_and_malformed_platform_map_are_rejected() -> None:
    with pytest.raises(ValueError, match="no 'linux/amd64' platform entry"):
        runner_image.labels_for_platform({"linux/arm64": {"config": {}}})
    with pytest.raises(ValueError, match="JSON object"):
        runner_image.labels_for_platform([{"os": "linux", "architecture": "amd64"}])


def test_absent_registry_labels_require_a_build() -> None:
    inputs = runner_image.Inputs(TOOLS_PATH, f"ghcr.io/example/rbe@{BASE_DIGEST}", ())
    assert runner_image.labels_for_platform({"os": "linux", "architecture": "amd64", "config": {}}) is None
    assert runner_image.needs_build(inputs, None)


def test_docker_registry_errors_propagate(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[:5] == ["docker", "buildx", "imagetools", "inspect", f"ghcr.io/example/runner@{RUNNER_DIGEST}"]
        assert args[-2:] == ["--format", "{{json .Image}}"]
        raise subprocess.CalledProcessError(1, args, stderr="unauthorized")

    monkeypatch.setattr(runner_image.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError) as error:
        runner_image.pinned_runner_labels(repo)
    assert error.value.stderr == "unauthorized"


def test_verify_rejects_a_stale_pre_pin_identity(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_nix(monkeypatch)
    with pytest.raises(SystemExit, match="runner image inputs changed"):
        runner_image.main(["verify", "--root", str(repo), "--key", EXPECTED_KEY])


def test_inputs_cli_writes_the_identity_and_handoff_values(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mock_nix(monkeypatch)
    output = tmp_path / "github-output"
    runner_image.main(["inputs", "--root", str(repo), "--output", str(output)])
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    inputs = runner_image.load_inputs(repo)
    assert values == {"key": inputs.identity, "tools_path": TOOLS_PATH, "base_ref": inputs.base_ref}


def test_plan_cli_reuses_the_pinned_image_only_when_its_label_matches(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inputs = runner_image.compute_inputs(repo, TOOLS_PATH)

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "nix":
            assert args == ["nix", "eval", "--raw", runner_image.TOOLS_ATTR]
            stdout = TOOLS_PATH + "\n"
        else:
            assert args[4] == f"ghcr.io/example/runner@{RUNNER_DIGEST}"
            stdout = json.dumps(
                {
                    "os": "linux",
                    "architecture": "amd64",
                    "config": {"Labels": {runner_image.INPUTS_LABEL: inputs.identity}},
                }
            )
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runner_image.subprocess, "run", run)
    output = tmp_path / "github-output"
    runner_image.main(["plan", "--root", str(repo), "--output", str(output)])
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert values["needs_build"] == "false"


if __name__ == "__main__":
    pytest_bazel.main()
