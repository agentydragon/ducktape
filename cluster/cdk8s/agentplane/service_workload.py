"""The Deployment skeleton every Agentplane service shares: its own ServiceAccount, one container in
the house Pod shape, the Service in front of it, and the environment's replica profile and PDB by
default. A service can supply its own profile when its ownership model requires it.

It builds into the caller's scope rather than a construct of its own: a Deployment's selector is
its construct path (`cdk8s.io/metadata.addr`), and a selector cannot change on a live Deployment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from cdk8s import ApiObject, ApiObjectMetadata, Duration, JsonPatch
from cdk8s_plus_34 import (
    Container,
    ContainerPort,
    ContainerProps,
    ContainerResources,
    ContainerSecurityContextProps,
    Deployment,
    EnvValue,
    ImagePullPolicy,
    PodSecurityContextProps,
    Probe,
    Secret,
    Service,
    ServiceAccount,
    k8s,
)
from constructs import Construct
from more_itertools import one

from cluster.cdk8s import node_scheduling, pod_policy
from cluster.cdk8s.agentplane import telemetry
from cluster.cdk8s.agentplane.environment import Environment, ReplicaProfile
from cluster.cdk8s.service_ref import ServiceRef


@dataclass(frozen=True)
class ServiceWorkload:
    account: ServiceAccount
    deployment: Deployment
    container: Container
    # Volume names `project_tokens` has patched in; the Deployment's own `volumes` omit them.
    _projected: list[str] = field(default_factory=list, init=False)

    def project_tokens(self, volume: str, mount_path: str, audiences: Mapping[str, str]) -> None:
        """Mounts a rotating workload token for each `{file: audience}` under `mount_path`, apart
        from the API-server token. cdk8s-plus has no projected ServiceAccount token volume.

        Call it after every other mount of the container: while the container has none, its patches
        replace the volume and mount arrays, which drops a mount added afterwards."""
        deployment = ApiObject.of(self.deployment)
        token_volume = k8s.Volume(
            name=volume,
            projected=k8s.ProjectedVolumeSource(
                sources=[
                    k8s.VolumeProjection(
                        service_account_token=k8s.ServiceAccountTokenProjection(
                            audience=audience, expiration_seconds=3600, path=path
                        )
                    )
                    for path, audience in audiences.items()
                ]
            ),
        )
        mount = k8s.VolumeMount(name=volume, mount_path=mount_path, read_only=True)
        # A patch appending to an absent array fails, so with no earlier volume the first one creates the
        # array. The Pod's volumes come from its containers' mounts, which `container.mounts` lists
        # (`deployment.volumes` stays empty until synthesis); neither lists these patches.
        if self._projected or self.container.mounts:
            deployment.add_json_patch(JsonPatch.add("/spec/template/spec/volumes/-", token_volume))
            deployment.add_json_patch(JsonPatch.add("/spec/template/spec/containers/0/volumeMounts/-", mount))
        else:
            deployment.add_json_patch(JsonPatch.add("/spec/template/spec/volumes", [token_volume]))
            deployment.add_json_patch(JsonPatch.add("/spec/template/spec/containers/0/volumeMounts", [mount]))
        self._projected.append(volume)


def add_service_workload(
    scope: Construct,
    env: Environment,
    *,
    serves: list[ServiceRef],
    container: str,
    image: str,
    readiness: Probe,
    liveness: Probe,
    resources: ContainerResources,
    termination_grace: Duration,
    startup: Probe | None = None,
    extra_ports: list[ContainerPort] | None = None,
    replica_profile: ReplicaProfile | None = None,
    args: list[str] | None = None,
    env_variables: dict[str, EnvValue] | None = None,
    migrate: ContainerProps | None = None,
    annotations: dict[str, str] | None = None,
    image_pull_policy: ImagePullPolicy = ImagePullPolicy.IF_NOT_PRESENT,
    tolerate_control_plane: bool = False,
    create_service: bool = True,
    traffic_distribution: str | None = None,
    placement: node_scheduling.Placement | None = None,
    telemetry_enabled: bool = False,
    registry_secret_name: str | None = None,
    automount_service_account_token: bool = True,
    read_only_root_filesystem: bool = False,
) -> ServiceWorkload:
    """`serves` identify the Pod ports and shared name/labels. Normally build their Service here;
    a separately reconciled Service can instead use the same refs. Mount into the returned container."""
    name = one({ref.name for ref in serves})
    labels = one({ref.pods for ref in serves}).selector
    # Several Service ports may target one listener; keep its first name.
    ports: dict[int, ContainerPort] = {}
    for ref in serves:
        ports.setdefault(ref.pod_port, ref.container_port())
    replicas = replica_profile or env.replicas
    # API services call TokenReview; static workloads can opt out of both token mounts.
    account = ServiceAccount(
        scope,
        "account",
        metadata=ApiObjectMetadata(name=name, namespace=env.namespace),
        automount_token=automount_service_account_token,
    )
    deployment = Deployment(
        scope,
        "deployment",
        metadata=ApiObjectMetadata(name=name, namespace=env.namespace, labels=labels, annotations=annotations),
        pod_metadata=ApiObjectMetadata(
            labels={**labels, **(telemetry.EMITTER_SERVICE_CLASS_LABELS if telemetry_enabled else {})}
        ),
        replicas=replicas.count,
        strategy=replicas.strategy,
        min_ready=replicas.min_ready,
        termination_grace_period=termination_grace,
        service_account=account,
        # Independent of the ServiceAccount's own automount_token, and also False by default.
        automount_service_account_token=automount_service_account_token,
        docker_registry_auth=(
            Secret.from_secret_name(scope, "images-creds", registry_secret_name)
            if registry_secret_name is not None
            else None
        ),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
        # A migration failure blocks startup; no migration command has a retry loop.
        init_containers=[migrate] if migrate is not None else None,
    )
    service_environment = (
        {name: EnvValue.from_value(value) for name, value in telemetry.service_environment(env.namespace, name).items()}
        if telemetry_enabled
        else {}
    )
    conflicting_environment = set(service_environment) & set(env_variables or {})
    if conflicting_environment:
        names = ", ".join(sorted(conflicting_environment))
        raise ValueError(f"service telemetry environment conflicts with workload environment: {names}")
    main = deployment.add_container(
        name=container,
        image=image,
        image_pull_policy=image_pull_policy,
        args=args,
        env_variables={**(env_variables or {}), **service_environment},
        ports=[*ports.values(), *(extra_ports or [])],
        readiness=readiness,
        liveness=liveness,
        startup=startup,
        resources=resources,
        # API services' writes remain unaudited; static workloads opt into a read-only root.
        security_context=ContainerSecurityContextProps(read_only_root_filesystem=read_only_root_filesystem),
    )
    # With the database (cnpg_conventions R5).
    if placement is not None:
        pod_policy.place(deployment, placement, tolerate_control_plane=tolerate_control_plane)
    pod_policy.harden(deployment)
    if create_service:
        service = Service(
            scope,
            "service",
            metadata=ApiObjectMetadata(name=name, namespace=env.namespace, labels=labels),
            selector=deployment,
            ports=[ref.service_port() for ref in serves],
        )
        if traffic_distribution is not None:
            # cdk8s-plus-34's Service props do not expose this core Service field.
            ApiObject.of(service).add_json_patch(JsonPatch.add("/spec/trafficDistribution", traffic_distribution))
    if replicas.pdb_min_available is not None:
        k8s.KubePodDisruptionBudget(
            scope,
            "pdb",
            metadata=k8s.ObjectMeta(name=name, namespace=env.namespace),
            spec=k8s.PodDisruptionBudgetSpec(
                min_available=k8s.IntOrString.from_number(replicas.pdb_min_available),
                selector=k8s.LabelSelector(match_labels=labels),
            ),
        )
    return ServiceWorkload(account=account, deployment=deployment, container=main)
