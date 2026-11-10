"""A bare-k3s Agentplane stack on one RBE action, for tests to drive.

Starts k3s with kube-proxy and no Cilium, loads the Bazel-built service images, applies the composed
manifest and yields the app's address and the test operator's ID token once every Deployment is Available.
It fails at the first Deployment that does not, with the Pod states and logs of whatever did not start.

The test process reaches the app (and Dex) by their in-cluster Service names, which it resolves
through `/etc/hosts`: OIDC redirects name them. Dex's
certificate comes from a CA made per stack, which `McpFixtureAccess.ca_bundle` adds to the system roots.
In MCP fixtures the app and standalone MCP edge use loopback origins, served by port-forwards, so
their plain HTTP URLs satisfy the OAuth endpoint contract.
"""

import contextlib
import contextvars
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from python.runfiles import Runfiles

from agentplane.system_tests.deployment import certificates, process_traces, rendered_fixture
from agentplane.system_tests.deployment.operator_tokens import OperatorTokens, dex_id_token
from agentplane.system_tests.deployment.virtualization import Virtualization, VirtualizationFactory
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.port_forward import PortForward
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.k3s import baseline, pki_infrastructure
from agentplane.system_tests.k3s.baseline import K3s, run
from agentplane.system_tests.k3s.namespaces import CREDENTIALS_NAMESPACE
from agentplane.testing import tls
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

DEPLOY_TIMEOUT_SECONDS = 480
# Marks the `/etc/hosts` lines a stack wrote, which the next stack on a recycled runner replaces.
HOSTS_MARKER = "# k3s-env"
# Outlasts the longest suite, whose `enormous` size allows 3600 s.
OPERATOR_TOKEN_LIFETIME = timedelta(hours=1)


@tracer.start_as_current_span("system_test.pki.fixture_ca")
def _openssl_ca(work: Path) -> tuple[str, str]:
    key, cert = work / "ca.key", work / "ca.crt"
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
    run(
        "openssl",
        "req",
        "-x509",
        "-new",
        "-key",
        str(key),
        "-days",
        "3",
        "-subj",
        "/CN=agentplane-egress-root-ca",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-out",
        str(cert),
    )
    return cert.read_text(), key.read_text()


@dataclass(frozen=True)
class McpFixtureAccess:
    # The system roots and the CA of Dex's certificate.
    ca_bundle: Path
    # The Secret with the operator's Dex login.
    operator_secret: str
    # The standalone edge's MCP endpoint, at the origin its OAuth server names itself by.
    mcp_edge_url: str


@dataclass(frozen=True)
class StackInputs:
    fixture: rendered_fixture.RenderedFixture
    binary: Path
    image_layouts: dict[str, Path]
    pki_image_layouts: dict[str, Path]
    vm_image_layouts: dict[str, Path]
    guest_image_layouts: dict[str, Path]


@dataclass(frozen=True)
class Stack:
    k3s: K3s
    cluster: Cluster
    base_url: str
    # The test operator's ID token, which the app accepts as a bearer.
    token: str
    mcp_fixtures: McpFixtureAccess | None


def _mcp_inputs(work: Path, dex_host: str) -> rendered_fixture.McpInputs:
    ca = tls.make_ca("test-k3s-fixture-ca")
    cert, key = tls.issue_leaf(ca, dex_host, work)
    # Free ports now; port-forwards listen once the app and MCP edge are Available.
    with socket.socket() as probe, socket.socket() as app_probe:
        probe.bind(("127.0.0.1", 0))
        app_probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        app_port = app_probe.getsockname()[1]
    return rendered_fixture.McpInputs(
        dex_ca_cert=ca.cert_pem.decode(),
        dex_cert=cert.read_text(),
        dex_key=key.read_text(),
        mcp_edge_origin=f"http://127.0.0.1:{port}",
        app_origin=f"http://127.0.0.1:{app_port}",
    )


@contextlib.contextmanager
def _port_forward(k3s: K3s, namespace: str, service: str, origin: str, port: int) -> Iterator[None]:
    """Forward the loopback `origin` to `service`'s `port` from when kubectl says it listens."""
    local = urlsplit(origin)
    if local.hostname is None or local.port is None:
        raise ValueError(f"port-forward origin needs a hostname and port: {origin!r}")
    with PortForward(
        (str(k3s.binary), "kubectl", "--kubeconfig", str(k3s.kubeconfig), "port-forward", "-n", namespace),
        resource=f"service/{service}",
        local_address=local.hostname,
        local_port=local.port,
        remote_port=port,
        readiness_timeout_s=30,
        output=sys.stdout.write,
    ):
        yield


@tracer.start_as_current_span("system_test.network.resolve")
def _resolve_on_node(k3s: K3s, fixture: rendered_fixture.RenderedFixture) -> None:
    """Resolve the Services a browser reaches by name for this process, as the cluster DNS does for Pods,
    replacing what the last stack on a recycled runner wrote."""
    hosts = Path("/etc/hosts")
    kept = [line for line in hosts.read_text().splitlines() if not line.endswith(HOSTS_MARKER)]
    for service in fixture.browser_services:
        address = k3s.kubectl(
            "get", "service", "-n", fixture.namespace, service.name, "-o", "jsonpath={.spec.clusterIP}"
        )
        kept.append(f"{address} {service.host} {service.fqdn} {HOSTS_MARKER}")
    # Rewritten in place: the file may be a mount.
    hosts.write_text("\n".join(kept) + "\n")


def _deployment_images(objects: list[dict[str, Any]]) -> set[str]:
    return {
        container["image"]
        for doc in objects
        if doc["kind"] == "Deployment"
        for container in (
            doc["spec"]["template"]["spec"].get("initContainers", []) + doc["spec"]["template"]["spec"]["containers"]
        )
    }


def _ready_fixture_postgres_pod(
    pods: list[dict[str, Any]], replica_sets: list[dict[str, Any]], deployment_uid: str
) -> dict[str, Any]:
    replica_set_uids = {
        replica_set["metadata"]["uid"]
        for replica_set in replica_sets
        if any(
            owner.get("controller") is True and owner.get("kind") == "Deployment" and owner.get("uid") == deployment_uid
            for owner in replica_set["metadata"].get("ownerReferences", [])
        )
    }
    ready_pods = [
        pod
        for pod in pods
        if pod["status"].get("phase") == "Running"
        and not pod["metadata"].get("deletionTimestamp")
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod["status"].get("conditions", [])
        )
        and any(
            owner.get("controller") is True
            and owner.get("kind") == "ReplicaSet"
            and owner.get("uid") in replica_set_uids
            for owner in pod["metadata"].get("ownerReferences", [])
        )
    ]
    if len(ready_pods) != 1:
        raise RuntimeError(
            "expected one Ready, non-terminating fixture PostgreSQL Pod owned by Deployment/postgres, "
            f"found {len(ready_pods)}"
        )
    return ready_pods[0]


def _baseline_evidence(
    k3s: K3s,
    fixture: rendered_fixture.RenderedFixture,
    models: rendered_fixture.Models,
    pki: list[dict[str, Any]],
    marker: dict[str, Any],
    warm: bool,
    virtualization: Virtualization | None,
) -> dict[str, Any]:
    pods = json.loads(k3s.kubectl("get", "pods", "-n", fixture.namespace, "-o", "json"))["items"]
    deployment = json.loads(k3s.kubectl("get", "deployment", "postgres", "-n", fixture.namespace, "-o", "json"))
    replica_sets = json.loads(k3s.kubectl("get", "replicasets", "-n", fixture.namespace, "-o", "json"))["items"]
    postgres = _ready_fixture_postgres_pod(pods, replica_sets, deployment["metadata"]["uid"])
    controllers = {
        doc["metadata"]["name"]: k3s.kubectl(
            "get", "deployment", doc["metadata"]["name"], "-n", "cert-manager", "-o", "jsonpath={.metadata.uid}"
        )
        for doc in pki
        if doc["kind"] == "Deployment"
    }
    vm_controllers = virtualization.baseline_identity(k3s) if virtualization is not None else {}
    if warm and vm_controllers != marker.get("vm_controller_uids", {}):
        raise RuntimeError("retained virtualization identities changed; refusing to mark this baseline clean")
    marker["vm_controller_uids"] = vm_controllers
    if warm and controllers != marker["pki_controller_uids"]:
        raise RuntimeError("retained PKI controller identities changed; refusing to mark this baseline clean")
    marker["pki_controller_uids"] = controllers
    return {
        "outcome": "warm" if warm else "cold",
        "fingerprint": marker["fingerprint"],
        "k3s": marker["identity"],
        "namespace_uid": k3s.kubectl("get", "namespace", fixture.namespace, "-o", "jsonpath={.metadata.uid}"),
        "postgres_pod_uid": postgres["metadata"]["uid"],
        "root_certificate_uid": k3s.kubectl(
            "get", "certificate", "test-runner-root", "-n", "cert-manager", "-o", "jsonpath={.metadata.uid}"
        ),
        "root_secret_uid": k3s.kubectl(
            "get", "secret", "test-runner-root", "-n", "cert-manager", "-o", "jsonpath={.metadata.uid}"
        ),
        "collector_namespace_uid": k3s.kubectl("get", "namespace", "monitoring", "-o", "jsonpath={.metadata.uid}"),
        "pki_controller_uids": controllers,
        "vm_controller_uids": vm_controllers,
        "workspace_storage_class_uid": (virtualization.storage_class_uid(k3s) if virtualization is not None else None),
        "layers_in_memory": not fixture.vm_sandboxes,
        "model_endpoint": models.url if isinstance(models, rendered_fixture.LocalModel) else None,
    }


@tracer.start_as_current_span("system_test.stack.baseline.clean")
def _baseline_clean(k3s: K3s, fixture: rendered_fixture.RenderedFixture) -> None:
    """Wait for fixture-owned resources to vanish, leaving only the infrastructure baseline."""
    namespaces = [doc["metadata"]["name"] for doc in fixture.objects if doc["kind"] == "Namespace"]
    if set(namespaces) != {fixture.namespace, CREDENTIALS_NAMESPACE, kubernetes_accounts_namespace(fixture.namespace)}:
        raise RuntimeError(f"unexpected fixture namespaces: {namespaces}")
    sandboxes = json.loads(k3s.kubectl("get", "sandboxes", "-n", fixture.namespace, "-o", "json"))["items"]
    if sandboxes:
        raise RuntimeError(f"fixture left {len(sandboxes)} Sandboxes; refusing to recycle it")
    owned_pvs = [
        item["metadata"]["name"]
        for item in json.loads(k3s.kubectl("get", "pv", "-o", "json"))["items"]
        if item.get("spec", {}).get("claimRef", {}).get("namespace") in namespaces
    ]
    # Submit both fixture-owned namespace deletions before waiting, so their controller
    # reconciliation overlaps. Keep monitoring alive until the fixture services have stopped.
    with tracer.start_as_current_span(
        "system_test.stack.namespaces.delete", attributes={"k8s.namespace.names": namespaces}
    ):
        k3s.kubectl("delete", "namespace", *namespaces, "--wait=true", "--timeout=60s")
    for name in owned_pvs:
        with tracer.start_as_current_span("system_test.stack.volume.delete", attributes={"k8s.pv.name": name}):
            k3s.kubectl("delete", "pv", name, "--ignore-not-found=true", "--wait=true", "--timeout=60s")
    with tracer.start_as_current_span(
        "system_test.stack.namespace.delete", attributes={"k8s.namespace.name": "monitoring"}
    ):
        k3s.kubectl("delete", "namespace", "monitoring", "--ignore-not-found=true", "--wait=true", "--timeout=60s")
    certificates.cleanup_fixture_certificates(k3s.kubectl, fixture.objects, namespaces)
    cluster_objects = [
        doc for doc in fixture.objects if doc["kind"] != "Namespace" and not doc["metadata"].get("namespace")
    ]
    cluster_yaml = yaml.safe_dump_all(cluster_objects)
    k3s.kubectl("delete", "-f", "-", "--ignore-not-found=true", "--wait=true", input_text=cluster_yaml)
    # Deprecated API reads (Kyverno ClusterPolicy) emit warnings even for absent objects. Resource
    # verification must inspect stdout, while preserving stderr warnings in the test diagnostics.
    remaining = k3s.kubectl(
        "get", "-f", "-", "--ignore-not-found=true", "-o", "name", input_text=cluster_yaml, stdout_only=True
    )
    if remaining:
        raise RuntimeError(f"fixture-owned cluster resources survived cleanup: {remaining}")
    dynamic_selector = (
        "app.kubernetes.io/managed-by=agentplane-policy-controller,"
        "policy.agentplane.allegedly.works/policy-namespace=systest"
    )
    k3s.kubectl("delete", "clusterrolebindings", "-l", dynamic_selector, "--ignore-not-found=true", "--wait=true")
    if k3s.kubectl("get", "clusterrolebindings", "-l", dynamic_selector, "-o", "name"):
        raise RuntimeError("policy-controller ClusterRoleBindings survived cleanup")
    for namespace in [*namespaces, "monitoring"]:
        if k3s.kubectl("get", "namespace", namespace, "--ignore-not-found=true", "-o", "name"):
            raise RuntimeError(f"namespace {namespace} still exists after cleanup")
    remaining_pvs = json.loads(k3s.kubectl("get", "pv", "-o", "json"))["items"]
    if any(item.get("spec", {}).get("claimRef", {}).get("namespace") in namespaces for item in remaining_pvs):
        raise RuntimeError("fixture-owned PersistentVolume survived cleanup")


def _certify_baseline(
    k3s: K3s,
    fixture: rendered_fixture.RenderedFixture,
    virtualization: Virtualization | None,
    marker: dict[str, Any],
    resources: contextlib.ExitStack,
    volumes: contextlib.ExitStack,
) -> int:
    with tracer.start_as_current_span("system_test.stack.resources.close"):
        resources.close()
    _baseline_clean(k3s, fixture)
    if virtualization is not None:
        virtualization.clean(k3s)
    # Namespace deletion releases all VM users. A failed detach must leave the marker dirty.
    with tracer.start_as_current_span("system_test.stack.volumes.release"):
        volumes.close()
    with tracer.start_as_current_span("system_test.stack.baseline.certify"):
        marker["state"] = "clean"
        baseline.write_marker(marker)
    return virtualization.released_devices if virtualization is not None else 0


@contextlib.contextmanager
def running_stack(
    inputs: StackInputs,
    *,
    models: rendered_fixture.Models,
    validate_vm_shutdown: Callable[[], bool],
    persist_infrastructure: bool,
    virtualization_factory: VirtualizationFactory | None,
) -> Iterator[Stack]:
    """Create fresh fixtures; retain infrastructure only with the executor's lifecycle permission."""
    crds = sorted((Runfiles.CreateOrRaise().root() / "ducktape/agentplane/crds/manifests").glob("crd-*.yaml"))
    fixture = inputs.fixture
    # Virtualized suites keep image layers on disk: a 2026-10-30 tmpfs trial beside six VM guests
    # lost an executor without a reliable warm-start gain.
    uses_virtualization = fixture.vm_sandboxes or fixture.firecracker_sandboxes
    if fixture.firecracker_sandboxes and os.environ.get("AGENTPLANE_K3S_REQUIRE_KVM") != "1":
        raise ValueError("the Firecracker fixture requires a KVM-enabled executor")
    if fixture.firecracker_sandboxes and persist_infrastructure:
        raise ValueError("the Firecracker fixture does not retain executor snapshots")
    cert_manager, trust_manager = pki_infrastructure.releases()
    pki_images = inputs.pki_image_layouts
    if uses_virtualization != (virtualization_factory is not None):
        raise ValueError("VM/Firecracker deployment and lifecycle extension do not match")
    work = Path(tempfile.mkdtemp())
    virtualization = virtualization_factory(work) if virtualization_factory is not None else None
    releases = virtualization.releases if virtualization is not None else []
    infrastructure_images = inputs.vm_image_layouts
    fixture_images = inputs.image_layouts
    guest_images = inputs.guest_image_layouts
    if uses_virtualization != bool(guest_images):
        raise ValueError("VM/Firecracker deployment and guest image inputs do not match")
    for objects, images in [(cert_manager + trust_manager, pki_images), (releases, infrastructure_images)]:
        if missing := _deployment_images(objects) - images.keys():
            raise ValueError(f"Infrastructure workloads lack OCI layouts for: {sorted(missing)}")

    k3s = K3s(work, inputs.binary)
    with (
        baseline.invalidate_snapshot_on_action_error(enabled=persist_infrastructure),
        contextlib.ExitStack() as trace_cleanup,
        contextlib.ExitStack() as block_cleanup,
        contextlib.ExitStack() as cleanup,
    ):
        with tracer.start_as_current_span("system_test.stack.setup") as setup:
            traces = trace_cleanup.enter_context(
                process_traces.ProcessTraces(k3s.node_ip, undeclared_outputs_dir() / "process-spans.jsonl")
            )
            # A VM stack's layers would leave too little memory for its guests (README § Image layers in memory).
            baseline_warm = False
            if persist_infrastructure:
                # The retained daemon writes outside this action's outputs, even on cold start.
                # Register before attach so a failed baseline shutdown also leaves its logs.
                trace_cleanup.callback(baseline.capture_retained_logs, k3s.data_dir, undeclared_outputs_dir())
                k3s, baseline_marker, baseline_warm = baseline.open_baseline(
                    inputs.binary,
                    baseline.fingerprint(
                        inputs.binary,
                        crds,
                        cert_manager
                        + trust_manager
                        + releases
                        + (virtualization.baseline_objects() if virtualization is not None else []),
                        pki_images | infrastructure_images,
                        layers_in_memory=not uses_virtualization,
                        helpers={
                            "certificates.py": Path(certificates.__file__),
                            **(virtualization.baseline_helpers() if virtualization is not None else {}),
                        },
                    ),
                    layers_in_memory=not uses_virtualization,
                )
            else:
                # An executor can restore a retained snapshot even when this action opts out.
                # Stop its exact owned server before resetting the ordinary stack's state.
                baseline.stop_previous()
                process = k3s.start(
                    layers_in_memory=not uses_virtualization,
                    preserve_image_store=not fixture.firecracker_sandboxes,
                    database_in_memory=fixture.firecracker_sandboxes,
                )
                cleanup.callback(k3s.stop, process)
            setup.add_event("node Ready")
            k3s.report_storage("before-import")
            cleanup.callback(k3s.report_storage, "final")
            cleanup.enter_context(k3s.sample_working_sets())
            cluster = Cluster(
                kubeconfig=k3s.kubeconfig, namespace=fixture.namespace, kubectl_command=(str(k3s.binary), "kubectl")
            )

            def remove_work() -> None:
                if virtualization is None or virtualization.closed:
                    shutil.rmtree(work, ignore_errors=True)
                else:
                    logger.error("preserving %s because a fixture loop device did not detach", work)

            block_cleanup.callback(remove_work)
            if virtualization is not None:
                block_cleanup.enter_context(virtualization)
            with tracer.start_as_current_span("system_test.collector.expose"):
                k3s.kubectl(
                    "apply",
                    "-f",
                    "-",
                    input_text=yaml.safe_dump_all(process_traces.collector_objects(k3s.node_ip, traces)),
                )
            setup.add_event("trace collector ready")
            ca_cert, ca_key = _openssl_ca(work)
            cluster_ca = Path(k3s.data_dir / "server/tls/server-ca.crt").read_text()
            system_roots = Path("/etc/ssl/certs/ca-certificates.crt").read_text()
            mcp = _mcp_inputs(work, fixture.mcp_fixtures.dex_host) if fixture.mcp_fixtures is not None else None
            operator_tokens = OperatorTokens(fixture.operator_tokens) if fixture.operator_tokens is not None else None
            with tracer.start_as_current_span("system_test.fixture.configure"):
                fixture = rendered_fixture.fill(
                    fixture,
                    rendered_fixture.RuntimeInputs(
                        ca_cert=ca_cert, ca_key=ca_key, system_roots=system_roots, cluster_ca=cluster_ca
                    ),
                    models,
                    mcp,
                    operator_tokens.public_key if operator_tokens is not None else None,
                )
                if virtualization is not None:
                    virtualization.configure(fixture.objects)
            deployment_images = _deployment_images(fixture.objects)
            if used := deployment_images & guest_images.keys():
                raise ValueError(f"Guest images are used by Deployments: {sorted(used)}")
            if missing := deployment_images - fixture_images.keys():
                raise ValueError(f"Fixture workloads lack OCI layouts for: {sorted(missing)}")
            install_baseline = not baseline_warm
            # A later group may already have an ID alias while CRI is still publishing its tag.
            # Register every current runfile image before any of the import-group sweeps.
            declared_images = pki_images | infrastructure_images | fixture_images | guest_images
            k3s.protect_image_layouts(declared_images)
            # A recycled node can hold another stack's images, e.g. the MCP fixtures on a Pod stack.
            k3s.remove_undeclared_fixture_images(declared_images.keys())
            if virtualization is not None and install_baseline:
                k3s.import_images(infrastructure_images)
            cleanup.callback(certificates.report, k3s.kubectl, cert_manager + trust_manager)
            # Import the next group's images during apply/readiness work for the preceding group. A single
            # worker preserves blob deduplication and joins before k3s teardown, including on setup failure.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-import") as importing:
                pki_import = (
                    importing.submit(contextvars.copy_context().run, k3s.import_images, pki_images)
                    if install_baseline
                    else None
                )
                if virtualization is not None and install_baseline:
                    virtualization.install(k3s)
                    setup.add_event("KubeVirt and Kyverno applied")
                # Applying a workload before its image is imported would pull it from the registry.
                if pki_import is not None:
                    pki_import.result()
                fixture_import = importing.submit(contextvars.copy_context().run, k3s.import_images, fixture_images)
                pki_infrastructure.prepare(k3s.kubectl, cert_manager, trust_manager, warm=baseline_warm)
                setup.add_event("cert-manager and trust-manager ready")
                fixture_import.result()
                setup.add_event("fixture images imported" if guest_images else "images imported")
                if guest_images:
                    # The VM root image is this stack's largest cold import. Sampling proceeds while
                    # the importer and the rest of setup work; joining happens at fixture teardown.
                    outputs = undeclared_outputs_dir()
                    cleanup.enter_context(
                        k3s.profile_containerd("profile", 15, outputs / "containerd-import.cpu.pprof")
                    )
                    cleanup.enter_context(
                        k3s.profile_containerd("trace", 5, outputs / "containerd-import.runtime.trace")
                    )
                guest_import = importing.submit(contextvars.copy_context().run, k3s.import_images, guest_images)
                if fixture.firecracker_sandboxes:
                    # The raw boot disk's cold unpack competes with PostgreSQL initialization and
                    # k3s SQLite writes on the KVM worker. Finish it before starting app workloads.
                    guest_import.result()
                with tracer.start_as_current_span("system_test.crds.apply"):
                    for crd in crds:
                        k3s.kubectl("apply", "-f", str(crd))
                if virtualization is not None:
                    virtualization.wait_for_apis(k3s)
                setup.add_event("CRDs applied")
                # Namespaces first, then CRD-backed and the rest; one apply tolerates ordering within a kind only.
                namespaces = [d for d in fixture.objects if d["kind"] == "Namespace"]
                rest = [d for d in fixture.objects if d["kind"] != "Namespace"]
                with tracer.start_as_current_span("system_test.manifests.apply"):
                    k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(namespaces))
                    if virtualization is not None:
                        cleanup.enter_context(virtualization.provisioning(cluster))
                        setup.add_event("workspace provisioner ready")
                    print("apply:", k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(rest)))
                    logger.info("applied %s objects", len(rest))
                    setup.add_event("manifests applied")
                certificates.wait_for_issuance(
                    k3s.kubectl, fixture.namespace, ("cluster-internal-ca-bundle", "agentplane-workload-ca-bundle")
                )
                setup.add_event("runner certificates issued and trust distributed")
                # Guest images serve Sandboxes created after stack readiness, so they can import during apply.
                guest_import.result()
                if guest_images:
                    setup.add_event("images imported")

            failed: list[str] = []
            deployments = [d["metadata"]["name"] for d in rest if d["kind"] == "Deployment"]
            deadline = time.monotonic() + DEPLOY_TIMEOUT_SECONDS
            for name in deployments:
                remaining = max(5, int(deadline - time.monotonic()))
                with tracer.start_as_current_span("system_test.deployment.ready", attributes={"deployment.name": name}):
                    out = k3s.kubectl(
                        "wait", "--for=condition=Available", f"deployment/{name}", "-n", fixture.namespace,
                        f"--timeout={remaining}s", check=False,
                    )  # fmt: skip
                    if "condition met" not in out:
                        failed.append(name)
            if failed:
                print(k3s.report(fixture.namespace))
                raise RuntimeError(f"Deployments not Available: {failed}")
            setup.add_event("Deployments Available")
            if virtualization is not None:
                virtualization.wait_until_ready(k3s)
                setup.add_event("KubeVirt and Kyverno ready")
            _resolve_on_node(k3s, fixture)
            access = None
            token = None
            if mcp is not None and fixture.mcp_fixtures is not None:
                ca_bundle = work / "ca-certificates.crt"
                ca_bundle.write_text(system_roots + mcp.dex_ca_cert)
                cleanup.enter_context(
                    _port_forward(
                        k3s,
                        fixture.namespace,
                        fixture.mcp_fixtures.mcp_edge_service,
                        mcp.mcp_edge_origin,
                        fixture.mcp_fixtures.mcp_edge_port,
                    )
                )
                cleanup.enter_context(
                    _port_forward(
                        k3s,
                        fixture.namespace,
                        fixture.mcp_fixtures.app_service,
                        mcp.app_origin,
                        fixture.mcp_fixtures.app_port,
                    )
                )
                access = McpFixtureAccess(
                    ca_bundle=ca_bundle,
                    operator_secret=fixture.mcp_fixtures.operator_secret,
                    mcp_edge_url=f"{mcp.mcp_edge_origin}/mcp",
                )
                token = dex_id_token(fixture.mcp_fixtures.operator_token_grant, ca_bundle=ca_bundle)
            if operator_tokens is not None:
                token = operator_tokens.mint(lifetime=OPERATOR_TOKEN_LIFETIME)
            if token is None:
                raise ValueError("the fixture neither pins an operator-token key nor runs Dex")
            print("memory:", run("free", "-m"))
            setup.add_event("stack ready")
            if virtualization is not None:
                cleanup.enter_context(virtualization.capture(k3s, fixture.namespace, validate_vm_shutdown))
        persistence_output = undeclared_outputs_dir() / "persistence-suite.json"
        if persist_infrastructure:
            persistence_evidence = _baseline_evidence(
                k3s, fixture, models, cert_manager + trust_manager, baseline_marker, baseline_warm, virtualization
            )
            persistence_output.write_text(json.dumps(persistence_evidence))
        yield Stack(k3s=k3s, cluster=cluster, base_url=fixture.app_origin, token=token, mcp_fixtures=access)
        with tracer.start_as_current_span("system_test.stack.diagnostics"):
            print(k3s.report(fixture.namespace))
            if virtualization is not None:
                (undeclared_outputs_dir() / "vm-sandboxes.txt").write_text(
                    virtualization.report(k3s, fixture.namespace)
                )
        if persist_infrastructure:
            # Stop the provisioner, forwards, profilers and guest log capture first. Their checks
            # must pass before we can certify the baseline, while the API and retained node stay live.
            with tracer.start_as_current_span("system_test.stack.cleanup"):
                released_devices = _certify_baseline(
                    k3s, fixture, virtualization, baseline_marker, cleanup, block_cleanup
                )
                persistence_evidence["workspace_loop_devices_released"] = released_devices
                persistence_evidence["cleanup"] = "complete"
                persistence_output.write_text(json.dumps(persistence_evidence))
                with tracer.start_as_current_span("system_test.stack.traces.close"):
                    trace_cleanup.close()
