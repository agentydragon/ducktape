import pytest_bazel

from haku.dispatch import result_tokens
from haku.dispatch.k8s_jobs import job_name

_REQUEST = {
    "prompt": ("Clone https://github.com/agentydragon/ducktape, run ruff over loom/, and fix lint findings."),
    "zone": "zai",
    "model": "glm-5.2-anthropic",
    "max_budget_usd": 1.5,
    "idempotency_key": "lint-loom-1",
}


async def test_requires_haku_token(client):
    # Missing header: 401 or 403 depending on the FastAPI version's HTTPBearer.
    assert (await client.post("/jobs", json=_REQUEST)).status_code in (401, 403)
    response = await client.post("/jobs", json=_REQUEST, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


async def test_dispatch_and_result_roundtrip(client, haku_headers, stamper, keys):
    created = await client.post("/jobs", json=_REQUEST, headers=haku_headers)
    assert created.status_code == 200, created.text
    job = created.json()
    name = job_name("lint-loom-1")
    assert job["id"] == name
    assert job["status"] == "created"
    assert keys.minted == [name]
    stamped = stamper.jobs[("haku-sandbox-zai", name)]
    assert stamped["litellm_key"] == f"sk-job-{name}"

    # Worker turns in the result with its job-scoped token.
    token = result_tokens.mint("hmac-secret", name)
    submitted = await client.post(
        f"/jobs/{name}/result",
        json={"result": "Fixed 3 findings; branch pushed.", "exit_code": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "completed"

    # L0 reads results via its read-only SQL role, not this API — the
    # submission response is the only HTTP surface carrying the result.
    assert submitted.json()["result"] == "Fixed 3 findings; branch pushed."


async def test_post_is_idempotent(client, haku_headers, keys):
    first = await client.post("/jobs", json=_REQUEST, headers=haku_headers)
    second = await client.post("/jobs", json=_REQUEST, headers=haku_headers)
    assert first.json()["id"] == second.json()["id"]
    assert len(keys.minted) == 1


async def test_unknown_zone_rejected(client, haku_headers):
    response = await client.post("/jobs", json=_REQUEST | {"zone": "oai"}, headers=haku_headers)
    assert response.status_code == 422
    assert "unknown zone" in response.json()["detail"]


async def test_model_outside_zone_allowlist_rejected(client, haku_headers):
    response = await client.post("/jobs", json=_REQUEST | {"model": "gpt-5.6-sol-chatgpt"}, headers=haku_headers)
    assert response.status_code == 422


async def test_credential_material_rejected_before_classifier(client, haku_headers, keys):
    bad = _REQUEST | {"prompt": "Use ghp_" + "A1b2C3d4" * 5 + " to push.", "idempotency_key": "x1"}
    response = await client.post("/jobs", json=bad, headers=haku_headers)
    assert response.status_code == 403
    assert "credential material" in response.json()["verdict"]["reason"]
    assert keys.minted == []


async def test_classifier_rejection_surfaces_reason(client, haku_headers, keys, classifier_verdict):
    classifier_verdict.allowed = False
    classifier_verdict.reason = "brief names the operator's employer"
    response = await client.post("/jobs", json=_REQUEST, headers=haku_headers)
    assert response.status_code == 403
    assert response.json()["verdict"]["reason"] == "brief names the operator's employer"
    assert keys.minted == []


async def test_wrong_result_token_rejected(client, haku_headers):
    created = await client.post("/jobs", json=_REQUEST, headers=haku_headers)
    name = created.json()["id"]
    response = await client.post(
        f"/jobs/{name}/result",
        json={"result": "spoofed", "exit_code": 0},
        headers={"Authorization": f"Bearer {result_tokens.mint('hmac-secret', 'job-other')}"},
    )
    assert response.status_code == 401


async def test_kill_revokes_key_and_deletes_job(client, haku_headers, stamper, keys):
    created = await client.post("/jobs", json=_REQUEST, headers=haku_headers)
    name = created.json()["id"]
    killed = await client.delete(f"/jobs/{name}", headers=haku_headers)
    assert killed.json()["status"] == "killed"
    assert keys.revoked == [name]
    assert stamper.jobs == {}


if __name__ == "__main__":
    pytest_bazel.main()
