import base64
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import (
    ArtifactGeneratorSpecArtifacts,
    ArtifactGeneratorSpecArtifactsCopy,
)

from cluster.cdk8s.artifact_generators import artifact
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    ConfigMapArgs,
    GeneratorOptions,
    RenderedDirectory,
    SecretGeneratorArgs,
    artifact_directories,
    artifact_directory,
    flux_kustomization,
    kustomizations_chart,
    kustomize_kustomization,
)
from util.bazel.runfiles import get_required_path

_HEALTH_CHECKS = [KustomizationSpecHealthChecks(kind="Deployment", name="test-app", namespace="test-ns")]


def _raw_kustomization(chart: Chart, *, wait: bool, health_checks: list[KustomizationSpecHealthChecks] | None) -> None:
    """Built directly, not through `flux_kustomization`, so its own guard does not run."""
    Kustomization(
        chart,
        "test-app",
        metadata=ApiObjectMetadata(name="test-app"),
        spec=KustomizationSpec(
            source_ref=KustomizationSpecSourceRef(kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="test-repo"),
            path="./test-app",
            interval="10m",
            prune=True,
            wait=wait,
            health_checks=health_checks,
        ),
    )


def test_wait_with_health_checks_fails_synth() -> None:
    chart = kustomizations_chart(Cdk8sTesting.app())
    _raw_kustomization(chart, wait=True, health_checks=_HEALTH_CHECKS)
    with pytest.raises(Exception, match="Kustomization/test-app"):
        Cdk8sTesting.synth(chart)


@pytest.mark.parametrize(("wait", "health_checks"), [(False, _HEALTH_CHECKS), (True, None)])
def test_wait_or_health_checks_alone_synthesizes(
    wait: bool, health_checks: list[KustomizationSpecHealthChecks] | None
) -> None:
    chart = kustomizations_chart(Cdk8sTesting.app())
    _raw_kustomization(chart, wait=wait, health_checks=health_checks)
    (rendered,) = Cdk8sTesting.synth(chart)
    assert rendered["spec"]["wait"] is wait


def test_rendered_directory_refuses_a_second_decryption() -> None:
    directory = RenderedDirectory(artifact=artifact("test-app", "test/app"), decryption=None)
    with pytest.raises(ValueError, match="decryption="):
        flux_kustomization(kustomizations_chart(Cdk8sTesting.app()), "test-app", directory, decryption=SOPS_DECRYPTION)


def test_artifact_directory_is_the_first_copy_of_an_artifact_with_bases() -> None:
    assert artifact_directory(artifact("test-app", "test/app", "test/base")) == "test/app"


def test_artifact_maps_one_source_file_into_its_consumer_directory() -> None:
    result = artifact("test-app", "test/app", files={"secrets/shared/test.yaml": "test/app/test.sops.yaml"})
    assert artifact_directories(result) == ["test/app"]
    assert artifact_directory(result) == "test/app"
    assert [(copy.from_, copy.to) for copy in result.copy] == [
        ("@repo/test/app/**", "@artifact/test/app/"),
        ("@repo/secrets/shared/test.yaml", "@artifact/test/app/test.sops.yaml"),
    ]


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("secrets/shared/*.yaml", "test/app/test.sops.yaml"),
        ("secrets/../public.yaml", "test/app/test.sops.yaml"),
        ("secrets/shared/test.yaml", "test/app/../outside.yaml"),
    ],
)
def test_artifact_rejects_file_copy_path_traversal(source: str, destination: str) -> None:
    with pytest.raises(ValueError, match=r"repository file path|file destination"):
        artifact("test-app", "test/app", files={source: destination})


@pytest.mark.parametrize(
    "copy",
    [
        ArtifactGeneratorSpecArtifactsCopy(from_="@repo/test/app/**", to="@artifact/test/other/"),
        ArtifactGeneratorSpecArtifactsCopy(from_="@repo/test/app/*.yaml", to="@artifact/test/app/"),
        ArtifactGeneratorSpecArtifactsCopy(from_="@repo/test/app/**", to="@artifact/test/app/", exclude=["*.md"]),
        ArtifactGeneratorSpecArtifactsCopy(from_="@repo/secrets/../test.yaml", to="@artifact/test/app/test.sops.yaml"),
        ArtifactGeneratorSpecArtifactsCopy(from_="@repo/secrets/test.yaml", to="@artifact/test/app/../outside.yaml"),
    ],
)
def test_artifact_directory_refuses_a_partial_or_moved_copy(copy: ArtifactGeneratorSpecArtifactsCopy) -> None:
    # The helper reports the offending first copy; the "copies nothing" error does not.
    with pytest.raises(ValueError, match=r"first\.from_="):
        artifact_directory(ArtifactGeneratorSpecArtifacts(name="test-app", origin_revision="@repo", copy=[copy]))


@pytest.mark.parametrize("directory", ["../outside", "test/app/../outside", "test/*.yaml"])
def test_artifact_rejects_unsafe_directory_paths(directory: str) -> None:
    with pytest.raises(ValueError, match="artifact directory paths"):
        artifact("test-app", directory)


def test_generator_entry_without_a_namespace_raises() -> None:
    with pytest.raises(ValueError, match="unplaced="):
        kustomize_kustomization(resources=[], config_map_generator=[ConfigMapArgs(name="test-config", namespace=None)])


def test_secret_generator_without_a_namespace_raises() -> None:
    with pytest.raises(ValueError, match="secretGenerator entries"):
        kustomize_kustomization(
            resources=[],
            secret_generator=[SecretGeneratorArgs(name="test-secret", namespace=None, files=["credentials.yaml"])],
        )


@pytest.mark.parametrize(
    ("namespace", "entry_namespace", "namespaced_by_overlay"),
    [("test-ns", None, False), (None, "test-ns", False), (None, None, True)],
    ids=["kustomization", "entry", "overlay"],
)
def test_generator_entry_placed_in_a_namespace_passes(
    namespace: str | None, entry_namespace: str | None, namespaced_by_overlay: bool
) -> None:
    kustomize_kustomization(
        resources=[],
        namespace=namespace,
        config_map_generator=[ConfigMapArgs(name="test-config", namespace=entry_namespace)],
        namespaced_by_overlay=namespaced_by_overlay,
    )


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("", "at least 1 character"),
        ("bad=key", "match pattern"),
        ("with space", "match pattern"),
        ("line\nbreak", "match pattern"),
        ("trailing\n", "match pattern"),
        ("directory/key", "match pattern"),
        ("bad:key", "match pattern"),
        ("a" * 254, "at most 253 characters"),
    ],
)
def test_config_map_data_rejects_invalid_keys(key: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ConfigMapArgs(name="test-config", namespace="test-ns", data={key: "value"})


@pytest.mark.parametrize("key", ["config.yaml", "file_name.conf", "UPPER-Case_01", ".env", "a" * 253])
def test_config_map_data_accepts_valid_file_keys(key: str) -> None:
    assert ConfigMapArgs(name="test-config", namespace="test-ns", data={key: "value"}).data == {key: "value"}


@pytest.mark.parametrize("key", [".", "...", "..anything"])
def test_config_map_data_rejects_directory_traversal_keys(key: str) -> None:
    with pytest.raises(ValueError, match="must not be"):
        ConfigMapArgs(name="test-config", namespace="test-ns", data={key: "value"})


def test_config_map_data_serializes_to_kustomize_literals() -> None:
    data = {
        "config.yaml": "credentials: user=token==\nnext: true\n",
        "empty": "",
        "quoted": '"keep space "',
        "single-quoted": "'literal quotes'",
    }
    entry = ConfigMapArgs(
        name="test-config",
        namespace="test-ns",
        data=data,
        files=["file.conf"],
        options=GeneratorOptions(disable_name_suffix_hash=True),
    )
    rendered = kustomize_kustomization(resources=[], config_map_generator=[entry])
    assert rendered["configMapGenerator"] == [
        {
            "name": "test-config",
            "namespace": "test-ns",
            "options": {"disableNameSuffixHash": True},
            "files": ["file.conf"],
            "literals": [
                "config.yaml=credentials: user=token==\nnext: true\n",
                "empty=",
                "quoted='\"keep space \"'",
                "single-quoted=\"'literal quotes'\"",
            ],
        }
    ]
    assert entry.data == data


def test_config_map_data_preserves_values_through_kustomize(tmp_path: Path) -> None:
    data = {
        "config.yaml": "credentials: user=token==\nnext: true\n",
        "empty": "",
        "quoted": '"keep space "',
        "single-quoted": "'literal quotes'",
        "query_instruction": "Instruct: retrieve relevant passages\nQuery: ",
    }
    entry = ConfigMapArgs(
        name="test-config",
        namespace="test-ns",
        data=data,
        files=["file.conf"],
        options=GeneratorOptions(disable_name_suffix_hash=True),
    )
    (tmp_path / "file.conf").write_text("file-backed content\n")
    (tmp_path / "kustomization.yaml").write_text(
        yaml.safe_dump(kustomize_kustomization(resources=[], config_map_generator=[entry]))
    )

    result = subprocess.run(
        [str(get_required_path("multitool/tools/kustomize/kustomize")), "build", str(tmp_path)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    [rendered] = yaml.safe_load_all(result.stdout)
    assert rendered["data"] == {**data, "file.conf": "file-backed content\n"}


def test_file_backed_config_map_omits_literals() -> None:
    rendered = kustomize_kustomization(
        resources=[], config_map_generator=[ConfigMapArgs(name="test-config", namespace="test-ns", files=["file.conf"])]
    )
    assert rendered["configMapGenerator"] == [{"name": "test-config", "namespace": "test-ns", "files": ["file.conf"]}]


def test_secret_generator_builds_encrypted_payload_for_flux_post_build_decryption(tmp_path: Path) -> None:
    # This synthetic payload stands in for the encrypted YAML at the exact path the
    # ArtifactGenerator copy places in the archive artifact. Kustomize packages it into Secret
    # data unchanged; the generated Flux Kustomization then decrypts that value after build.
    # This test never reads or prints the real encrypted credentials file.
    source_name = "session-backup.sops.yaml"
    source_content = "synthetic encrypted YAML payload\n"
    (tmp_path / source_name).write_text(source_content)
    (tmp_path / "workload.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "batch/v1",
                "kind": "CronJob",
                "metadata": {"name": "import", "namespace": "session-backup"},
                "spec": {
                    "schedule": "0 * * * *",
                    "jobTemplate": {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [{"name": "import", "image": "example.invalid/importer"}],
                                    "restartPolicy": "Never",
                                    "volumes": [
                                        {
                                            "name": "credentials",
                                            "secret": {"secretName": "session-backup-importer-credentials"},
                                        }
                                    ],
                                }
                            }
                        }
                    },
                },
            }
        )
    )
    (tmp_path / "kustomization.yaml").write_text(
        yaml.safe_dump(
            kustomize_kustomization(
                resources=["workload.yaml"],
                secret_generator=[
                    SecretGeneratorArgs(
                        name="session-backup-importer-credentials",
                        namespace="session-backup",
                        files=[f"session-backup.yaml={source_name}"],
                    )
                ],
            )
        )
    )

    result = subprocess.run(
        [str(get_required_path("multitool/tools/kustomize/kustomize")), "build", str(tmp_path)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    documents = {document["kind"]: document for document in yaml.safe_load_all(result.stdout)}
    secret = documents["Secret"]
    cron_job = documents["CronJob"]
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"].startswith("session-backup-importer-credentials-")
    assert secret["metadata"]["namespace"] == "session-backup"
    assert list(secret["data"]) == ["session-backup.yaml"]
    assert base64.b64decode(secret["data"]["session-backup.yaml"]).decode() == source_content
    [volume] = cron_job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
    assert volume["secret"]["secretName"] == secret["metadata"]["name"]


def test_empty_config_map_data_serializes_as_empty_literals() -> None:
    rendered = kustomize_kustomization(
        resources=[], config_map_generator=[ConfigMapArgs(name="test-config", namespace="test-ns", data={})]
    )
    assert rendered["configMapGenerator"] == [{"name": "test-config", "namespace": "test-ns", "literals": []}]


if __name__ == "__main__":
    pytest_bazel.main()
