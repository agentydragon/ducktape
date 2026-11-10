"""Only the named requesters get certificates from the Agentplane workload issuer, and only for their
own URIs; certificates from other issuers are not this policy's concern."""

from __future__ import annotations

import asyncio
import base64
import copy
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import cdk8s
import pytest
import pytest_bazel
import yaml
from cdk8s import Chart
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config import new_client_from_config
from more_itertools import one
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, kubernetes_apiserver
from cluster.cdk8s.agentplane import policy_controller, sandbox_controller, staging, workload_certificate_admission
from cluster.cdk8s.cert_manager.cluster_ca import WORKLOAD_ISSUER

Body = dict[str, Any]
Admits = Callable[[str, Body], Awaitable[bool]]

GROUP = "cert-manager.io"
VERSION = "v1"
NAMESPACE = "test-workload-sandboxes"
OTHER_NAMESPACE = "test-workload-other"
SANDBOX_CONTROLLER = ServiceAccountRef(namespace=NAMESPACE, name="test-sandbox-controller")
RECORD_ISSUER = ServiceAccountRef(namespace=NAMESPACE, name="test-policy-controller")
CREDENTIAL_ID = "0123456789abcdef0123456789abcdef"
OTHER_ACCOUNT = f"system:serviceaccount:{NAMESPACE}:test-other-account"
APPLIER = "test-manifest-applier"
CERT_MANAGER = ServiceAccountRef(namespace="test-cert-manager", name="test-cert-manager")
ISSUER_REF: Body = {"name": WORKLOAD_ISSUER.name, "kind": WORKLOAD_ISSUER.kind, "group": GROUP}
_PLURALS = {"Certificate": "certificates", "CertificateRequest": "certificaterequests"}


def _username(account: ServiceAccountRef) -> str:
    return f"system:serviceaccount:{account.namespace}:{account.name}"


def _runtime_uri(namespace: str) -> str:
    return f"spiffe://agentplane/environment-runtime/{namespace}/3b1c0f1e-7a52-4f0c-9d52-2d7a54f0b2a1/test-credential"


def _record_uri(namespace: str, name: str = "test-agent") -> str:
    return f"spiffe://agentplane/{namespace}/{name}/{CREDENTIAL_ID}"


def _certificate(*, uris: list[str], namespace: str = NAMESPACE, issuer: Body = ISSUER_REF) -> Body:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Certificate",
        "metadata": {"name": "test-certificate", "namespace": namespace},
        "spec": {
            "secretName": "test-certificate",
            "issuerRef": issuer,
            "uris": uris,
            "usages": ["digital signature", "server auth", "client auth"],
        },
    }


def _certificate_request(issuer: Body) -> Body:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "CertificateRequest",
        "metadata": {"name": "test-certificate-request", "namespace": NAMESPACE},
        "spec": {"issuerRef": issuer, "request": base64.b64encode(b"test-csr").decode()},
    }


def _with_spec(body: Body, **fields: object) -> Body:
    changed = copy.deepcopy(body)
    changed["spec"].update(fields)
    return changed


def _without_spec(body: Body, field: str) -> Body:
    changed = copy.deepcopy(body)
    del changed["spec"][field]
    return changed


# The client serializes a manifest dict as it would its own model, which is what its stubs name.
def _cert_manager_crds() -> list[Any]:
    runfiles = Runfiles.CreateOrRaise()
    return [
        yaml.safe_load(Path(str(runfiles.Rlocation(os.environ[variable]))).read_text())
        for variable in ("CERT_MANAGER_CERTIFICATE_CRD", "CERT_MANAGER_CERTIFICATEREQUEST_CRD")
    ]


async def _install_cert_manager_crds(server: KubernetesApiServer) -> None:
    extensions = k8s_client.ApiextensionsV1Api(server.api)
    names = []
    for crd in await asyncio.to_thread(_cert_manager_crds):
        await extensions.create_custom_resource_definition(crd)
        names.append(crd["metadata"]["name"])
    async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
        with attempt:
            for name in names:
                installed = await extensions.read_custom_resource_definition(name)
                assert any(c.type == "Established" and c.status == "True" for c in installed.status.conditions or [])


def _policy_object(documents: list[Body], kind: str) -> Body:
    return one(
        d for d in documents if d["kind"] == kind and d["metadata"]["name"] == workload_certificate_admission.POLICY
    )


@asynccontextmanager
async def _admission(server: KubernetesApiServer, documents: list[Body], namespaces: set[str]) -> AsyncIterator[Admits]:
    """Install the policy in `documents` and yield whether a dry-run create by a user is admitted."""
    await _install_cert_manager_crds(server)
    core = k8s_client.CoreV1Api(server.api)
    for namespace in namespaces:
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace)))
    admission = k8s_client.AdmissionregistrationV1Api(server.api)
    policy: Any = _policy_object(documents, "ValidatingAdmissionPolicy")
    binding: Any = _policy_object(documents, "ValidatingAdmissionPolicyBinding")
    await admission.create_validating_admission_policy(policy)
    await admission.create_validating_admission_policy_binding(binding)

    async def admitted(user: str, body: Body) -> bool:
        async with await new_client_from_config(config_file=str(server.kubeconfig)) as impersonating:
            impersonating.set_default_header("Impersonate-User", user)
            # Authorized as an administrator, so that admission alone decides.
            impersonating.set_default_header("Impersonate-Group", "system:masters")
            try:
                await k8s_client.CustomObjectsApi(impersonating).create_namespaced_custom_object(
                    GROUP, VERSION, body["metadata"]["namespace"], _PLURALS[body["kind"]], body, dry_run="All"
                )
            except ApiException as error:
                if error.status != 422:
                    raise
                return False
            return True

    yield admitted


async def _await_policy(admitted: Admits, user: str, refused: Body) -> None:
    # The API server loads a new policy in the background, and shows no sign of having loaded it.
    async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
        with attempt:
            assert not await admitted(user, refused)


async def test_only_named_requesters_get_workload_certificates(scratch_chart: Chart) -> None:
    workload_certificate_admission.add_workload_certificate_admission(
        scratch_chart, workload_credential_issuers={RECORD_ISSUER}, service_requester=APPLIER, cert_manager=CERT_MANAGER
    )
    documents = cast(list[Body], cdk8s.Testing.synth(scratch_chart))
    controller = _username(SANDBOX_CONTROLLER)
    runner = _certificate(uris=[_runtime_uri(NAMESPACE)])
    service = _certificate(uris=["spiffe://agentplane/test-service"])
    record_issuer = _username(RECORD_ISSUER)
    record = _certificate(uris=[_record_uri(NAMESPACE)])
    async with (
        kubernetes_apiserver() as server,
        _admission(server, documents, {NAMESPACE, OTHER_NAMESPACE}) as admitted,
    ):
        await _await_policy(admitted, OTHER_ACCOUNT, record)
        # cdk8s renders an issuerRef without a group; cert-manager defaults it.
        assert await admitted(record_issuer, _with_spec(record, issuerRef={k: ISSUER_REF[k] for k in ("name", "kind")}))
        assert await admitted(APPLIER, service)
        assert await admitted(record_issuer, record)
        assert await admitted(
            record_issuer, _certificate(uris=[_record_uri(OTHER_NAMESPACE)], namespace=OTHER_NAMESPACE)
        )
        assert await admitted(record_issuer, _with_spec(record, uris=[_record_uri(NAMESPACE, "test-box.scratch")]))
        assert await admitted(_username(CERT_MANAGER), _certificate_request(ISSUER_REF))

        refused = {
            # The Sandbox Controller requests records now; the Runtime URI form is gone.
            "a Runtime URI from the Sandbox Controller": (controller, runner),
            "a Runtime URI from another account": (OTHER_ACCOUNT, runner),
            "a service URI from the Sandbox Controller": (controller, service),
            "a service URI from another account": (OTHER_ACCOUNT, service),
            "a record URI without a group from another account": (
                OTHER_ACCOUNT,
                _with_spec(record, issuerRef={k: ISSUER_REF[k] for k in ("name", "kind")}),
            ),
            "a record URI with an extra segment": (
                record_issuer,
                _with_spec(record, uris=[f"{_record_uri(NAMESPACE)}/extra"]),
            ),
            "a Certificate without URIs": (record_issuer, _without_spec(record, "uris")),
            "a service URI under a path": (
                APPLIER,
                _with_spec(service, uris=["spiffe://agentplane/test-service/extra"]),
            ),
            "a CA": (record_issuer, _with_spec(record, isCA=True)),
            "a certificate-signing usage": (APPLIER, _with_spec(service, usages=["digital signature", "cert sign"])),
            "a record URI from the Sandbox Controller": (controller, record),
            "a record URI from the applier": (APPLIER, record),
            "a record URI from another account": (OTHER_ACCOUNT, record),
            "another namespace's record URI": (record_issuer, _with_spec(record, uris=[_record_uri(OTHER_NAMESPACE)])),
            "a record URI without a credential ID": (
                record_issuer,
                _with_spec(record, uris=[f"spiffe://agentplane/{NAMESPACE}/test-agent/test-credential"]),
            ),
            "a Runtime URI from the issuer": (record_issuer, runner),
            "a service URI from the issuer": (record_issuer, service),
            "a record URI beside a service URI": (
                record_issuer,
                _with_spec(record, uris=[_record_uri(NAMESPACE), "spiffe://agentplane/actions"]),
            ),
            "a CertificateRequest from the issuer": (record_issuer, _certificate_request(ISSUER_REF)),
        }
        for case, (user, body) in refused.items():
            assert not await admitted(user, body), case
            # Schema-valid, and admitted against another issuer: this policy alone refuses it.
            assert await admitted(
                user, _with_spec(body, issuerRef={"name": "cluster-internal-ca", "kind": "ClusterIssuer"})
            ), case

        # Other issuers, including a namespaced Issuer of the same name, are not this policy's concern.
        for issuer in (
            {"name": "cluster-internal-ca", "kind": "ClusterIssuer"},
            {"name": WORKLOAD_ISSUER.name, "kind": "Issuer"},
            {"name": WORKLOAD_ISSUER.name},
        ):
            assert await admitted(OTHER_ACCOUNT, _with_spec(record, issuerRef=issuer)), issuer
            assert await admitted(OTHER_ACCOUNT, _certificate_request(issuer)), issuer

        # A write that leaves the spec alone, as cert-manager's and the garbage collector's are, is
        # admitted from anyone; a spec change is checked like a create.
        certificates = k8s_client.CustomObjectsApi(server.api)
        async with await new_client_from_config(config_file=str(server.kubeconfig)) as impersonating:
            impersonating.set_default_header("Impersonate-User", record_issuer)
            impersonating.set_default_header("Impersonate-Group", "system:masters")
            await k8s_client.CustomObjectsApi(impersonating).create_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, "certificates", record
            )
        async with await new_client_from_config(config_file=str(server.kubeconfig)) as impersonating:
            impersonating.set_default_header("Impersonate-User", OTHER_ACCOUNT)
            impersonating.set_default_header("Impersonate-Group", "system:masters")
            other = k8s_client.CustomObjectsApi(impersonating)
            current = cast(
                Body,
                await certificates.get_namespaced_custom_object(
                    GROUP, VERSION, NAMESPACE, "certificates", "test-certificate"
                ),
            )
            labelled = copy.deepcopy(current)
            labelled["metadata"]["labels"] = {"test-label": "test-value"}
            current = cast(
                Body,
                await other.replace_namespaced_custom_object(
                    GROUP, VERSION, NAMESPACE, "certificates", "test-certificate", labelled
                ),
            )
            with pytest.raises(ApiException) as refused_update:
                await other.replace_namespaced_custom_object(
                    GROUP,
                    VERSION,
                    NAMESPACE,
                    "certificates",
                    "test-certificate",
                    _with_spec(current, uris=service["spec"]["uris"]),
                )
            assert refused_update.value.status == 422
        stored = await certificates.get_namespaced_custom_object(
            GROUP, VERSION, NAMESPACE, "certificates", "test-certificate"
        )
        assert cast(Body, stored)["spec"]["uris"] == record["spec"]["uris"]


async def test_staging_requesters_get_the_certificates_staging_renders(staging_manifests: list[Body]) -> None:
    """Flux's applier may write every workload Certificate staging renders and the Policy Controller a
    record's; neither may write the other's, and the Sandbox Controller writes none."""
    workload = [
        doc
        for doc in staging_manifests
        if doc["kind"] == "Certificate"
        and doc["spec"]["issuerRef"].get("name") == WORKLOAD_ISSUER.name
        and doc["spec"]["issuerRef"].get("kind") == WORKLOAD_ISSUER.kind
    ]
    assert workload, "staging renders no workload Certificate"
    applier = "system:serviceaccount:flux-system:kustomize-controller"
    controller = f"system:serviceaccount:{staging.ENV.namespace}:{sandbox_controller.NAME}"
    record_issuer = f"system:serviceaccount:{staging.ENV.namespace}:{policy_controller.NAME}"
    record = _certificate(uris=[_record_uri(staging.ENV.namespace)], namespace=staging.ENV.namespace)
    namespaces = {doc["metadata"]["namespace"] for doc in workload} | {staging.ENV.namespace}
    async with kubernetes_apiserver() as server, _admission(server, staging_manifests, namespaces) as admitted:
        await _await_policy(admitted, applier, record)
        assert await admitted(record_issuer, record)
        assert not await admitted(controller, record)
        for certificate in workload:
            assert await admitted(applier, certificate), certificate["metadata"]["name"]
            assert not await admitted(record_issuer, certificate), certificate["metadata"]["name"]
            assert not await admitted(controller, certificate), certificate["metadata"]["name"]


if __name__ == "__main__":
    pytest_bazel.main()
