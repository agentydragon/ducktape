"""Stamping tests against the REAL reviewed template from cluster/k8s —
a parity check that the template and the stamper agree on placeholders."""

import pytest_bazel

from haku.x.dispatch.k8s_jobs import job_name, render_job, render_secret
from util.bazel.runfiles import get_required_path

_TEMPLATE = get_required_path("_main/cluster/k8s/x/haku/dispatch/dispatcher/job-template.yaml").read_text()


def test_job_name_deterministic_and_dns_safe():
    name = job_name("ducktape-chore-2026-07-02-a")
    assert name == job_name("ducktape-chore-2026-07-02-a")
    assert name.startswith("job-")
    assert len(name) <= 63
    assert name != job_name("ducktape-chore-2026-07-02-b")


def _render() -> dict:
    return render_job(
        _TEMPLATE, name="job-0123456789abcdef", namespace="haku-sandbox-zai", zone="zai", model="glm-5.2-anthropic"
    )


def test_render_fills_every_placeholder():
    job = _render()
    assert "${" not in str(job)
    assert job["metadata"]["name"] == "job-0123456789abcdef"
    assert job["metadata"]["namespace"] == "haku-sandbox-zai"


def test_rendered_job_wires_per_job_secret():
    job = _render()
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    container = pod["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    assert env["ANTHROPIC_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "job-0123456789abcdef"
    assert env["RESULT_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "job-0123456789abcdef"
    assert env["MODEL"]["value"] == "glm-5.2-anthropic"
    # No secret value may appear inline in the pod spec (Haku-visible).
    assert all("value" not in env[k] for k in ["ANTHROPIC_AUTH_TOKEN", "RESULT_TOKEN"])
    assert pod["volumes"][0]["secret"]["secretName"] == "job-0123456789abcdef"


def test_render_secret_carries_prompt_and_credentials():
    secret = render_secret(
        name="job-0123456789abcdef",
        namespace="haku-sandbox-zai",
        prompt="Do the thing.",
        litellm_key="sk-per-job",
        result_token="deadbeef",
    )
    assert secret.metadata.name == "job-0123456789abcdef"
    assert secret.string_data == {
        "prompt.md": "Do the thing.",
        "ANTHROPIC_AUTH_TOKEN": "sk-per-job",
        "RESULT_TOKEN": "deadbeef",
    }


if __name__ == "__main__":
    pytest_bazel.main()
