"""Stamping validator-gated Jobs into zone namespaces.

The Job body comes from the reviewed template in
cluster/k8s/haku/dispatch/dispatcher/job-template.yaml — this module only
substitutes the per-job values, it never composes pod specs in code. The k8s
Job name is derived from the caller's idempotency key, so exactly-once creation
is enforced atomically by the API server (AlreadyExists on retry), with no
dispatcher bookkeeping.

Each job gets a same-named Secret carrying the prompt and the two dispatcher-
minted credentials (per-job LiteLLM key, result token) — secretKeyRef/mounts
only, since pod specs are Haku-visible via cluster-diagnostics-reader.
"""

import hashlib
import re
from typing import Any, cast

import yaml
from kubernetes_asyncio import client

from haku.dispatch.models import Zone


def job_name(idempotency_key: str) -> str:
    return f"job-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"


# Explicit token replacement, not string.Template: the template legitimately
# contains other $-tokens (the Flux {"$imagepolicy": ...} marker) that must
# survive verbatim.
_PLACEHOLDER = re.compile(r"\$\{[A-Z_]+\}")


def render_job(template_text: str, *, name: str, namespace: str, zone: Zone, model: str) -> dict:
    rendered = template_text
    for token, value in {
        "${JOB_NAME}": name,
        "${NAMESPACE}": namespace,
        "${ZONE}": str(zone),
        "${MODEL}": model,
    }.items():
        rendered = rendered.replace(token, value)
    if leftover := _PLACEHOLDER.search(rendered):
        raise ValueError(f"unfilled template placeholder {leftover.group()}")
    job: dict = yaml.safe_load(rendered)
    return job


def render_secret(*, name: str, namespace: str, prompt: str, litellm_key: str, result_token: str) -> client.V1Secret:
    return client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=name, namespace=namespace, labels={"app.kubernetes.io/name": "haku-zone-worker"}
        ),
        string_data={"prompt.md": prompt, "ANTHROPIC_AUTH_TOKEN": litellm_key, "RESULT_TOKEN": result_token},
    )


class ZoneJobStamper:
    def __init__(self, api_client: client.ApiClient, template_text: str) -> None:
        self._batch = client.BatchV1Api(api_client)
        self._core = client.CoreV1Api(api_client)
        self._template_text = template_text

    async def job_exists(self, namespace: str, name: str) -> bool:
        try:
            await self._batch.read_namespaced_job(name, namespace)
        except client.ApiException as e:
            if e.status == 404:
                return False
            raise
        return True

    async def create(
        self, *, name: str, namespace: str, zone: Zone, model: str, prompt: str, litellm_key: str, result_token: str
    ) -> None:
        secret = render_secret(
            name=name, namespace=namespace, prompt=prompt, litellm_key=litellm_key, result_token=result_token
        )
        try:
            await self._core.create_namespaced_secret(namespace, secret)
        except client.ApiException as e:
            # Leftover from a previous partial attempt (job creation failed
            # after the secret landed) — replace with the fresh credentials.
            if e.status != 409:
                raise
            await self._core.replace_namespaced_secret(name, namespace, secret)
        job = render_job(self._template_text, name=name, namespace=namespace, zone=zone, model=model)
        try:
            # The client serializes plain dicts natively; the stubs only admit
            # V1Job, hence the cast. The template dict IS the reviewed spec.
            await self._batch.create_namespaced_job(namespace, cast(Any, job))
        except client.ApiException as e:
            # Concurrent POST with the same idempotency key: the API server is
            # the arbiter — the other racer's Job (same name, same template)
            # already exists, which is the requested outcome.
            if e.status != 409:
                raise

    async def delete(self, namespace: str, name: str) -> None:
        for call in (
            lambda: self._batch.delete_namespaced_job(name, namespace, propagation_policy="Background"),
            lambda: self._core.delete_namespaced_secret(name, namespace),
        ):
            try:
                await call()
            except client.ApiException as e:
                if e.status != 404:
                    raise
