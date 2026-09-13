import hashlib
import io
import json
import zipfile

import install_dreo
import pytest
import pytest_bazel


def _archive(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _component_archive(version: str = install_dreo.VERSION) -> bytes:
    return _archive(
        {
            "source/custom_components/dreo/manifest.json": json.dumps(
                {"domain": install_dreo.DOMAIN, "version": version}
            ),
            "source/custom_components/dreo/__init__.py": "# installed\n",
            "source/README.md": "not installed\n",
        }
    )


def test_install_rejects_wrong_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(install_dreo, "SHA256", "0" * 64)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        install_dreo.install(tmp_path, b"not the release")


def test_install_rejects_path_traversal(tmp_path, monkeypatch):
    payload = _archive({"../escaped": "bad"})
    monkeypatch.setattr(install_dreo, "SHA256", hashlib.sha256(payload).hexdigest())
    with pytest.raises(ValueError, match="unsafe archive member"):
        install_dreo.install(tmp_path, payload)
    assert not (tmp_path.parent / "escaped").exists()


def test_install_replaces_component_and_only_copies_component(tmp_path, monkeypatch):
    target = tmp_path / "custom_components" / install_dreo.DOMAIN
    target.mkdir(parents=True)
    (target / "old.py").write_text("old")
    payload = _component_archive()
    monkeypatch.setattr(install_dreo, "SHA256", hashlib.sha256(payload).hexdigest())

    install_dreo.install(tmp_path, payload)

    assert not (target / "old.py").exists()
    assert (target / "__init__.py").read_text() == "# installed\n"
    assert not (tmp_path / "README.md").exists()


def test_install_rejects_unexpected_manifest_version(tmp_path, monkeypatch):
    payload = _component_archive(version="0.0.0")
    monkeypatch.setattr(install_dreo, "SHA256", hashlib.sha256(payload).hexdigest())
    with pytest.raises(ValueError, match="unexpected version"):
        install_dreo.install(tmp_path, payload)


if __name__ == "__main__":
    pytest_bazel.main()
