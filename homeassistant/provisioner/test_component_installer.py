import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
import pytest_bazel
from component_installer import initialize_component_config, install_component
from settings import ComponentConfig


def _archive(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _component(
    *,
    sha256: str,
    archive_path: str = "source/custom_components/example",
    install_dir: str = "example",
    version: str = "2.2.1",
    manifest_domain: str | None = "example",
    config_files: tuple[str, ...] = (),
) -> ComponentConfig:
    return ComponentConfig(
        version=version,
        url="https://example.test/component.zip",
        sha256=sha256,
        archive_path=archive_path,
        install_dir=install_dir,
        manifest_domain=manifest_domain,
        config_files=config_files,
    )


async def test_install_rejects_wrong_checksum(tmp_path: Path):
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        await install_component(tmp_path, b"not the release", _component(sha256="0" * 64))


async def test_install_rejects_path_traversal(tmp_path: Path):
    payload = _archive({"../escaped": "bad"})
    component = _component(sha256=hashlib.sha256(payload).hexdigest())

    with pytest.raises(ValueError, match="unsafe archive member"):
        await install_component(tmp_path, payload, component)

    assert not (tmp_path.parent / "escaped").exists()


async def test_install_replaces_component_and_only_copies_component(tmp_path: Path):
    target = tmp_path / "custom_components" / "example"
    target.mkdir(parents=True)
    (target / "old.py").write_text("old")
    payload = _archive(
        {
            "source/custom_components/example/manifest.json": json.dumps({"domain": "example", "version": "2.2.1"}),
            "source/custom_components/example/__init__.py": "# installed\n",
            "source/README.md": "not installed\n",
        }
    )
    component = _component(sha256=hashlib.sha256(payload).hexdigest())

    await install_component(tmp_path, payload, component)

    assert not (target / "old.py").exists()
    assert (target / "__init__.py").read_text() == "# installed\n"
    assert not (tmp_path / "README.md").exists()


async def test_install_supports_archive_root(tmp_path: Path):
    payload = _archive({"manifest.json": json.dumps({"version": "1.1.1"}), "__init__.py": "# installed\n"})
    component = _component(
        sha256=hashlib.sha256(payload).hexdigest(),
        archive_path=".",
        install_dir="root_component",
        version="1.1.1",
        manifest_domain=None,
    )

    await install_component(tmp_path, payload, component)

    assert (tmp_path / "custom_components/root_component/__init__.py").read_text() == "# installed\n"


async def test_install_rejects_unexpected_manifest(tmp_path: Path):
    payload = _archive(
        {"source/custom_components/example/manifest.json": json.dumps({"domain": "other", "version": "0.0.0"})}
    )
    component = _component(sha256=hashlib.sha256(payload).hexdigest())

    with pytest.raises(ValueError, match="unexpected version"):
        await install_component(tmp_path, payload, component)


async def test_initialize_config_creates_only_missing_files(tmp_path: Path):
    existing = tmp_path / "automations.yaml"
    existing.write_text("- id: keep-me\n")
    component = _component(sha256="0" * 64, config_files=("automations.yaml", "scripts.yaml", "scenes.yaml"))

    await initialize_component_config(tmp_path, component)

    assert existing.read_text() == "- id: keep-me\n"
    assert (tmp_path / "scripts.yaml").read_text() == "[]\n"
    assert (tmp_path / "scenes.yaml").read_text() == "[]\n"


if __name__ == "__main__":
    pytest_bazel.main()
