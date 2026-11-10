"""Exercise the WorkloadCredential schema through Kubernetes admission and CEL: the subject and
binding unions, and the transition rule that re-points a subject only together with a new binding."""

from typing import Any, cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
PLURAL = "workloadcredentials"
NAMESPACE = "workload-credential-admission"
FIRST_POD = "3b1c0f1e-7a52-4f0c-9d52-2d7a54f0b2a1"
SECOND_POD = "5d0e6c7a-1b2f-4c3d-8e9f-0a1b2c3d4e5f"
SUBJECT = {"serviceAccount": "test-agent"}
OTHER_SUBJECT = {"serviceAccount": "test-other-agent"}


class Records:
    def __init__(self, api: k8s_client.CustomObjectsApi) -> None:
        self.api = api

    async def create(self, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "WorkloadCredential",
            "metadata": {"name": name},
            "spec": spec,
        }
        return cast(
            dict[str, Any], await self.api.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL, body)
        )

    async def rejects_create(self, spec: dict[str, Any], name: str = "test-rejected") -> None:
        with pytest.raises(ApiException) as rejected:
            await self.create(name, spec)
        assert rejected.value.status == 422, f"{name} {spec}: {rejected.value.body!r}"

    async def replace_spec(self, record: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.api.replace_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, PLURAL, record["metadata"]["name"], {**record, "spec": spec}
            ),
        )

    async def rejects_spec(self, record: dict[str, Any], spec: dict[str, Any]) -> None:
        with pytest.raises(ApiException) as rejected:
            await self.replace_spec(record, spec)
        assert rejected.value.status == 422, f"{spec}: {rejected.value.body!r}"

    async def replace_status(self, record: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.api.replace_namespaced_custom_object_status(
                GROUP, VERSION, NAMESPACE, PLURAL, record["metadata"]["name"], {**record, "status": status}
            ),
        )


async def _unions(records: Records) -> None:
    identity = {"identity": {"name": "test-agent", "uid": FIRST_POD}}
    for name, spec in (
        ("test-standing", {"subject": SUBJECT}),
        ("test-identity", {"subject": identity}),
        (
            "test-pod",
            {"subject": SUBJECT, "binding": {"podUID": FIRST_POD}, "pod": {"name": "test-pod", "uid": FIRST_POD}},
        ),
        ("test-vmi", {"subject": SUBJECT, "binding": {"vmiUID": FIRST_POD}}),
        (
            "test-vmi-launched",
            {"subject": SUBJECT, "binding": {"vmiUID": FIRST_POD}, "pod": {"name": "test-launcher", "uid": SECOND_POD}},
        ),
    ):
        assert (await records.create(name, spec))["spec"] == spec
    invalid: tuple[dict[str, Any], ...] = (
        {},
        {"subject": {}},
        {"subject": {**SUBJECT, **identity}},
        {"subject": {"serviceAccount": ""}},
        {"subject": {"identity": {"name": "test-agent"}}},
        {"subject": {"identity": {"name": "test-agent", "uid": "not-a-uid"}}},
        {"subject": SUBJECT, "binding": {}},
        {"subject": SUBJECT, "binding": {"podUID": FIRST_POD, "vmiUID": SECOND_POD}},
        {"subject": SUBJECT, "binding": {"podUID": "not-a-uid"}},
        # The caller's Pod is the bound one, and there is none without a binding.
        {"subject": SUBJECT, "binding": {"podUID": FIRST_POD}, "pod": {"name": "test-pod", "uid": SECOND_POD}},
        {"subject": SUBJECT, "pod": {"name": "test-pod", "uid": FIRST_POD}},
    )
    for spec in invalid:
        await records.rejects_create(spec)
    # The Certificate and Secret names derived from the record's name must stay within 253.
    await records.rejects_create({"subject": SUBJECT}, name="a" * 201)


async def _transitions(records: Records) -> None:
    record = await records.create("test-transitions", {"subject": SUBJECT})
    # A record without a binding is replaced, never re-pointed.
    await records.rejects_spec(record, {"subject": OTHER_SUBJECT})

    record = await records.replace_spec(record, {"subject": SUBJECT, "binding": {"podUID": FIRST_POD}})
    for spec in (
        # The same incarnation cannot change who it acts as.
        {"subject": OTHER_SUBJECT, "binding": {"podUID": FIRST_POD}},
        # Clearing the binding is not a new one.
        {"subject": OTHER_SUBJECT},
    ):
        await records.rejects_spec(record, spec)
    record = await records.replace_spec(record, {"subject": OTHER_SUBJECT, "binding": {"podUID": SECOND_POD}})
    assert record["spec"]["subject"] == OTHER_SUBJECT

    # A VMI's launcher Pod is named once, and only a new binding names another.
    vmi = {"vmiUID": FIRST_POD}
    record = await records.replace_spec(record, {"subject": OTHER_SUBJECT, "binding": vmi})
    launcher = {"name": "test-launcher", "uid": SECOND_POD}
    record = await records.replace_spec(record, {"subject": OTHER_SUBJECT, "binding": vmi, "pod": launcher})
    await records.rejects_spec(
        record, {"subject": OTHER_SUBJECT, "binding": vmi, "pod": {**launcher, "uid": FIRST_POD}}
    )

    # The binding moves on its own: clearing it is how an owner revokes its incarnation.
    record = await records.replace_spec(record, {"subject": OTHER_SUBJECT})
    assert "binding" not in record["spec"]


async def _status(records: Records) -> None:
    record = await records.create("test-status", {"subject": SUBJECT, "binding": {"podUID": FIRST_POD}})
    status = {
        "observedGeneration": 1,
        "credentialID": "0123456789abcdef0123456789abcdef",
        "uri": f"spiffe://agentplane/{NAMESPACE}/test-status/0123456789abcdef0123456789abcdef",
        "secretName": "svid-test-status-0123456789abcdef0123456789abcdef",
        "acceptedBinding": {"podUID": FIRST_POD},
    }
    record = await records.replace_status(record, status)
    assert record["status"] == status
    with pytest.raises(ApiException) as rejected:
        await records.replace_status(record, {**status, "credentialID": "not-hex"})
    assert rejected.value.status == 422, rejected.value.body


async def test_workload_credential_admission() -> None:
    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=NAMESPACE)))
        records = Records(k8s_client.CustomObjectsApi(server.api))
        await _unions(records)
        await _transitions(records)
        await _status(records)


if __name__ == "__main__":
    pytest_bazel.main()
