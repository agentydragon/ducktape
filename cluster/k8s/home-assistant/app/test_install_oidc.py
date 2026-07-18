import hashlib
import io
import json
import zipfile

import install_oidc
import pytest
import pytest_bazel


def _archive(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_initialize_config_creates_only_missing_files(tmp_path):
    existing = tmp_path / "automations.yaml"
    existing.write_text("- id: keep-me\n")

    install_oidc.initialize_config(tmp_path)

    assert existing.read_text() == "- id: keep-me\n"
    assert (tmp_path / "scripts.yaml").read_text() == "[]\n"
    assert (tmp_path / "scenes.yaml").read_text() == "[]\n"


def test_install_rejects_wrong_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(install_oidc, "SHA256", "0" * 64)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        install_oidc.install(tmp_path, b"not the release")


def test_install_rejects_path_traversal(tmp_path, monkeypatch):
    payload = _archive({"../escaped": "bad", "manifest.json": "{}"})
    monkeypatch.setattr(install_oidc, "SHA256", hashlib.sha256(payload).hexdigest())
    with pytest.raises(ValueError, match="unsafe archive member"):
        install_oidc.install(tmp_path, payload)
    assert not (tmp_path.parent / "escaped").exists()


def test_install_replaces_component_after_validation(tmp_path, monkeypatch):
    target = tmp_path / "custom_components" / "auth_oidc"
    target.mkdir(parents=True)
    (target / "old.py").write_text("old")
    payload = _archive({"manifest.json": json.dumps({"version": install_oidc.VERSION}), "__init__.py": "# installed\n"})
    monkeypatch.setattr(install_oidc, "SHA256", hashlib.sha256(payload).hexdigest())
    install_oidc.install(tmp_path, payload)
    assert not (target / "old.py").exists()
    assert (target / "__init__.py").read_text() == "# installed\n"


if __name__ == "__main__":
    pytest_bazel.main()
