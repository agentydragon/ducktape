"""Tests for check_image_automation_webhook."""

from pathlib import Path

import pytest_bazel

from cluster.validation.image_automation import check_image_automation_webhook


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _repo(name: str) -> str:
    return (
        "apiVersion: image.toolkit.fluxcd.io/v1beta2\n"
        "kind: ImageRepository\n"
        f"metadata: {{name: {name}, namespace: flux-system}}\n"
    )


def _policy(name: str, repo: str) -> str:
    return (
        "apiVersion: image.toolkit.fluxcd.io/v1\n"
        "kind: ImagePolicy\n"
        f"metadata: {{name: {name}, namespace: flux-system}}\n"
        f"spec: {{imageRepositoryRef: {{name: {repo}}}}}\n"
    )


def _receiver(repos: list[str]) -> str:
    if repos:
        entries = "\n".join(
            f"    - {{apiVersion: image.toolkit.fluxcd.io/v1beta2, kind: ImageRepository, name: {r}}}" for r in repos
        )
        resources = f"  resources:\n{entries}\n"
    else:
        resources = "  resources: []\n"
    return (
        "apiVersion: notification.toolkit.fluxcd.io/v1\n"
        "kind: Receiver\n"
        "metadata: {name: github, namespace: flux-system}\n"
        "spec:\n"
        "  type: github\n"
        f"{resources}"
    )


def test_consistent_is_clean(tmp_path: Path) -> None:
    _write(tmp_path, "flux-image-automation-ghcr/foo.yaml", _repo("foo") + "---\n" + _policy("foo", "foo"))
    _write(tmp_path, "flux-webhook/github-webhook-receiver.yaml", _receiver(["foo"]))
    assert check_image_automation_webhook(tmp_path) == []


def test_repository_missing_from_webhook(tmp_path: Path) -> None:
    _write(tmp_path, "flux-image-automation-ghcr/foo.yaml", _repo("foo") + "---\n" + _policy("foo", "foo"))
    _write(tmp_path, "flux-webhook/github-webhook-receiver.yaml", _receiver([]))
    errors = check_image_automation_webhook(tmp_path)
    assert any("foo" in e and "github-webhook-receiver" in e for e in errors)


def test_stale_webhook_entry(tmp_path: Path) -> None:
    _write(tmp_path, "flux-image-automation-ghcr/foo.yaml", _repo("foo") + "---\n" + _policy("foo", "foo"))
    _write(tmp_path, "flux-webhook/github-webhook-receiver.yaml", _receiver(["foo", "gone"]))
    errors = check_image_automation_webhook(tmp_path)
    assert any("gone" in e for e in errors)


def test_dangling_policy_reference(tmp_path: Path) -> None:
    _write(tmp_path, "flux-image-automation-ghcr/bar.yaml", _policy("bar", "ghost"))
    _write(tmp_path, "flux-webhook/github-webhook-receiver.yaml", _receiver([]))
    errors = check_image_automation_webhook(tmp_path)
    assert any("bar" in e and "ghost" in e for e in errors)


if __name__ == "__main__":
    pytest_bazel.main()
