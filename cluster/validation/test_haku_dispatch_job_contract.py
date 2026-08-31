"""Contract tests for the reviewed Kubernetes Job template used by Haku dispatch."""

from pathlib import Path

import pytest_bazel

from haku.x.dispatch.k8s_jobs import render_job


def test_reviewed_job_template_renders_without_unfilled_placeholders(k8s_dir: Path) -> None:
    template = (k8s_dir / "x/haku/dispatch/dispatcher/job-template.yaml").read_text(encoding="utf-8")

    job = render_job(
        template, name="job-0123456789abcdef", namespace="haku-sandbox-zai", zone="zai", model="glm-5.2-anthropic"
    )

    assert "${" not in str(job)
    assert job["metadata"]["name"] == "job-0123456789abcdef"
    assert job["metadata"]["namespace"] == "haku-sandbox-zai"


def test_reviewed_job_template_keeps_credentials_in_the_per_job_secret(k8s_dir: Path) -> None:
    job = render_job(
        (k8s_dir / "x/haku/dispatch/dispatcher/job-template.yaml").read_text(encoding="utf-8"),
        name="job-0123456789abcdef",
        namespace="haku-sandbox-zai",
        zone="zai",
        model="glm-5.2-anthropic",
    )
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}

    assert pod["automountServiceAccountToken"] is False
    assert env["ANTHROPIC_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "job-0123456789abcdef"
    assert env["RESULT_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "job-0123456789abcdef"
    assert env["MODEL"]["value"] == "glm-5.2-anthropic"
    assert all("value" not in env[name] for name in ("ANTHROPIC_AUTH_TOKEN", "RESULT_TOKEN"))
    assert pod["volumes"][0]["secret"]["secretName"] == "job-0123456789abcdef"


if __name__ == "__main__":
    pytest_bazel.main()
