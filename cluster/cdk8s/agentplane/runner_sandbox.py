"""The runner's boxes, behind the egress path every agentplane box shares (sandbox_pod.py): the
runner SandboxTemplate (agentplane/sandbox_controller), which adds the harness runner and a state
volume, and the fence its Pods share. Each deployment names and images its own Pod execution
environments: a `RunnerTemplate` takes the SandboxTemplate name it offers and the image it runs, and
a deployment adds as many as it offers (staging one, on the ducktape tools image; the k3s fixture one,
on an image of its own). Every one of them shares this module's fence, which selects Pods by label.

A runner template names no ServiceAccount, and the environment creates none for them: the Sandbox
controller runs every Sandbox's Pod as the Sandbox's own account, which it owns and creates before
the Pod (agentplane/sandbox_controller/identity.go), so an account named in a template is never
used and would only invite the assumption that runner Pods share one.

The templates' container images carry the same `unset` placeholder tag as the app's Deployment:
kustomize's `images:` transformer patches by image name across every resource in the Kustomization,
so image-pins/ covers them too.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from cdk8s import ApiObject, ApiObjectMetadata, JsonPatch
from cdk8s_plus_34 import ConfigMap, k8s
from constructs import Construct

from agentplane.crd_group import GROUP, VERSION
from agentplane.runner import configuration
from agentplane.runner.configuration import (
    CONFIG_FILE,
    LAUNCH_FILE,
    Configuration,
    ContainerExecutor,
    Endpoint,
    Harness,
    PodEnvironment,
)
from agentplane.runner.platform_instructions import render_platform_instructions
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import (
    actions,
    egress,
    launcher_relay,
    llm_ingress,
    notifications,
    platform_routes,
    runner_vm,
    sandbox_pod,
    telemetry,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy, isolate_ingress
from cluster.cdk8s.service_ref import Pods, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile

# The fence's name, and the Pod name label every runner SandboxTemplate carries. Neither is an
# identity: runner Pods run as the Sandbox's own ServiceAccount (this module's docstring). The label is
# what the fence selects on, so a deployment's own template joins it without naming the fence.
NAME = "runner"
# Sandbox runner Pods can outlive their template revision. Keep the label they already carry so the
# network fences continue to select existing incarnations until they are naturally replaced.
_POD_LABEL_NAME = "agentplane-runner"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_STORAGE_CLASS = "piraeus-hdd-local"
_LABELS = {"app.kubernetes.io/name": _POD_LABEL_NAME}
# Shared by the Runner configuration, its container volumeMount, and the
# SandboxTemplate's state volume -- all three must name the same volume.
_STATE_VOLUME_NAME = "state"
# The runner disk image's `bin/` (//agentplane/runner:disk_image): the runner and the `agentplane`
# command, mounted read-only the way the VM guest mounts the same image's erofs. The container's
# command is the runner here, not the tools image's entrypoint.
_RUNNER_VOLUME_NAME = "runner"
_RUNNER_DIR = "/run/agentplane/runner"
_STATE_DIR = "/state"
_WORKSPACE_VOLUME_NAME = "workspace"
_WORKSPACE_DIR = f"{_STATE_DIR}/workspaces"
_HOME_VOLUME_NAME = "home"
_HOME_DIR = "/home/runner"
_TMP_VOLUME_NAME = "tmp"
_TMP_DIR = "/tmp"
_EXECUTOR_VOLUME_NAME = "codex-executor"
_EXECUTOR_DIR = "/run/agentplane/executor"
_EXECUTOR_SOCKET = f"{_EXECUTOR_DIR}/control.sock"
_EXECUTOR_URL = "ws://127.0.0.1:4501"
_EXECUTOR_CONTAINER_NAME = "codex-executor"
_CONFIG_VOLUME = "runner-config"
# The bearers a Session presents: the placeholders the agentplane-workload and the notifications
# EgressCredentials derive from their names, which the egress proxy substitutes.
_WORKLOAD_PLACEHOLDER = "agentplane-credential-agentplane-workload"
_NOTIFICATIONS_PLACEHOLDER = "agentplane-credential-agentplane-notifications-workload"


@dataclass(frozen=True)
class KeyFile:
    """One Secret a runner's workload containers read as a file: the Secret to project, where to mount
    it, the key's file there, and the environment variable naming that file for the runner and for the
    harness children it spawns. A deployment's caller supplies these (staging's BuildBuddy key is one);
    the template never names a credential itself, and never projects one into the egress sidecar, which
    holds a Session's own bearers."""

    secret_name: str
    mount_path: str
    file_name: str
    env_var: str

    @property
    def path(self) -> str:
        return f"{self.mount_path}/{self.file_name}"

    def volume(self) -> k8s.Volume:
        return k8s.Volume(
            name=self.secret_name,
            # 0440: readable through the workload's fsGroup, never world-readable.
            secret=k8s.SecretVolumeSource(secret_name=self.secret_name, default_mode=288),
        )

    def mount(self) -> k8s.VolumeMount:
        return k8s.VolumeMount(name=self.secret_name, mount_path=self.mount_path, read_only=True)

    def env(self) -> k8s.EnvVar:
        return k8s.EnvVar(name=self.env_var, value=self.path)


@dataclass(frozen=True)
class ServiceUrls:
    """The base URLs of the services a Session calls, as the platform instructions list them."""

    egress: str
    actions: str
    notifications: str


def service_urls(env: Environment, *, egress_api_service: ServiceRef | None = None) -> ServiceUrls:
    actions_service = actions.service(env.namespace)
    egress_api = egress_api_service or egress.agent_api(env.namespace)
    egress_port = f":{egress_api.port.number}" if egress_api.port.number != 80 else ""
    return ServiceUrls(
        egress=f"http://{egress_api.fqdn}{egress_port}",
        actions=f"http://{actions_service.fqdn}:{actions_service.port.number}",
        notifications=f"http://{notifications.service(env.namespace).fqdn}:8080",
    )


def platform_instructions(env: Environment, *, egress_api_service: ServiceRef | None = None) -> str:
    """The complete deployment-wide prompt: shared guidance with this environment's service URLs."""
    urls = service_urls(env, egress_api_service=egress_api_service)
    return render_platform_instructions(
        egress_api_url=urls.egress, actions_service_url=urls.actions, notifications_service_url=urls.notifications
    )


def agent_cli_environment(urls: ServiceUrls) -> dict[str, str]:
    """What the `agentplane` command reads (agentplane/docs/agent_cli.md): each group's base URL and
    the placeholder bearer the egress proxy swaps for the real credential."""
    return {
        "AGENTPLANE_ACTIONS_URL": urls.actions,
        "AGENTPLANE_ACTIONS_CREDENTIAL": _WORKLOAD_PLACEHOLDER,
        "AGENTPLANE_NOTIFICATIONS_URL": urls.notifications,
        "AGENTPLANE_NOTIFICATIONS_CREDENTIAL": _NOTIFICATIONS_PLACEHOLDER,
        "AGENTPLANE_EGRESS_URL": urls.egress,
        "AGENTPLANE_EGRESS_CREDENTIAL": _WORKLOAD_PLACEHOLDER,
    }


def pods(namespace: str) -> Pods:
    """Every runner Pod in one environment's namespace, whichever template it was stamped from."""
    return Pods(namespace=namespace, labels=tuple(_LABELS.items()))


def workspace_volume_spec(
    state_volume: dict[str, Any],
    *,
    layout_type: Literal["DirectoryTree", "RawDiskImage", "BlockFilesystem"],
    storage_class: str | None = None,
) -> dict[str, Any]:
    """The workspace claim follows the state claim, with layout-derived volume mode and optional class."""
    layout: dict[str, str] = {"type": layout_type}
    if layout_type != "DirectoryTree":
        layout["filesystem"] = "ext4"
    claim = {**state_volume["claim"], "volumeMode": "Block" if layout_type == "BlockFilesystem" else "Filesystem"}
    if storage_class is not None:
        if not storage_class:
            raise ValueError("storage_class cannot be empty")
        claim["storageClassName"] = storage_class
    return {"name": _WORKSPACE_VOLUME_NAME, "claim": claim, "layout": layout}


class RunnerSandbox(Construct):
    """The environment's Pod execution environment, plus the VM template and relay when enabled. Fleet
    network and route fences are added separately. A VM workspace can name a distinct class when its
    Block claim needs a different provisioner from the state filesystem."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        name: str,
        image: str,
        key_files: Sequence[KeyFile] = (),
        vm_workspace_storage_class: str | None = None,
        egress_api_service: ServiceRef | None = None,
        storage_class: str = _STORAGE_CLASS,
        runner_resources: k8s.ResourceRequirements | None = None,
        executor_resources: k8s.ResourceRequirements | None = None,
        sidecar_resources: k8s.ResourceRequirements | None = None,
        image_pull_secret_name: str | None = sandbox_pod.SECRET_NAME,
        trust_mounts: Sequence[k8s.VolumeMount] | None = None,
        control_service: ServiceRef | None = None,
        session_reconcile_concurrency: int | None = None,
    ) -> None:
        super().__init__(scope, id)
        if vm_workspace_storage_class is not None:
            if not vm_workspace_storage_class:
                raise ValueError("vm_workspace_storage_class cannot be empty")
            if not env.vm_sandboxes:
                raise ValueError("vm_workspace_storage_class requires vm_sandboxes")
        self.env = env
        namespace = env.namespace
        template = RunnerTemplate(
            self,
            "runner-template",
            env,
            name=name,
            image=image,
            key_files=key_files,
            storage_class=storage_class,
            runner_resources=runner_resources,
            executor_resources=executor_resources,
            sidecar_resources=sidecar_resources,
            image_pull_secret_name=image_pull_secret_name,
            trust_mounts=trust_mounts,
            control_service=control_service,
            egress_api_service=egress_api_service,
            session_reconcile_concurrency=session_reconcile_concurrency,
        )
        if env.vm_sandboxes:
            launcher_relay.LauncherRelayPolicy(
                self,
                "launcher-relay",
                namespace=namespace,
                runner_pod=template.spec["pod"],
                settings_config_map=template.add_launcher_relay_settings(name="launcher-relay-egress-sidecar"),
            )
            # VM claim storage is separate from the runner Pod's state claim. Copy it before
            # selecting the VM class so the Pod template keeps its own storage policy.
            state_volume = template.spec["stateVolume"]
            state_claim = {**state_volume["claim"], "volumeMode": "Filesystem"}
            if env.vm_storage_class is not None:
                state_claim["storageClassName"] = env.vm_storage_class
            vm_state_volume = {**state_volume, "claim": state_claim}
            workspace_storage_class = vm_workspace_storage_class
            if workspace_storage_class is None:
                workspace_storage_class = env.vm_storage_class
            runner_vm.RunnerVmTemplate(
                self,
                "runner-vm-template",
                env,
                labels={**_LABELS, **telemetry.EMITTER_RUNNER_CLASS_LABELS},
                state_volume=vm_state_volume,
                workspace_volume=workspace_volume_spec(
                    vm_state_volume, layout_type="BlockFilesystem", storage_class=workspace_storage_class
                ),
                platform_instructions=platform_instructions(env, egress_api_service=egress_api_service),
                harness_env=template.agent_cli_env,
                session_reconcile_concurrency=session_reconcile_concurrency,
            )

    def add_platform_resources(self) -> None:
        """Add the fleet runner network and platform-route fences."""
        namespace = self.env.namespace
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=pods(namespace).selector,
            ingress=isolate_ingress(),
            egress=[cilium.dns_egress(), egress.proxy(namespace).egress()],
        )
        platform_routes.PlatformRouteFences(self, "platform-routes", namespace=namespace, clients=pods(namespace))


class RunnerTemplate(Construct):
    """One Pod execution environment: the runner Pod and storage wiring, as SandboxTemplate `name`, on
    image `image`. `key_files` are the caller's Secrets to project into its workload containers; the
    egress sidecar holds a Session's bearers and gets none of them. Isolated Codex execution adds a
    companion container with its own PID namespace. A deployment adds one of these per execution
    environment it offers, and names it in the launch presets its app config publishes."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        name: str,
        image: str,
        key_files: Sequence[KeyFile] = (),
        storage_class: str = _STORAGE_CLASS,
        runner_resources: k8s.ResourceRequirements | None = None,
        executor_resources: k8s.ResourceRequirements | None = None,
        sidecar_resources: k8s.ResourceRequirements | None = None,
        image_pull_secret_name: str | None = sandbox_pod.SECRET_NAME,
        trust_mounts: Sequence[k8s.VolumeMount] | None = None,
        control_service: ServiceRef | None = None,
        egress_api_service: ServiceRef | None = None,
        session_reconcile_concurrency: int | None = None,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.name = name
        self.image = image
        self.key_files = tuple(key_files)
        self.config_map_name = f"{name}-config"
        self.egress_api_service = egress_api_service
        self.storage_class = storage_class
        self.runner_resources = runner_resources
        self.executor_resources = executor_resources
        self.sidecar_resources = sidecar_resources
        self.image_pull_secret_name = image_pull_secret_name
        self.trust_mounts = trust_mounts if trust_mounts is not None else sandbox_pod.proxy_trust_mounts()
        self.control_service = control_service
        self.session_reconcile_concurrency = session_reconcile_concurrency
        self.agent_cli_env = agent_cli_environment(service_urls(env, egress_api_service=egress_api_service))
        if not env.isolated_codex_executor and executor_resources is not None:
            raise ValueError("executor_resources cannot be set when isolated Codex execution is disabled")
        # The SandboxTemplate's spec: its `pod`, and the `stateVolume` the VM template shares.
        self.spec = self._add_sandbox_template()

    def _execution_mounts(self) -> list[k8s.VolumeMount]:
        return [
            k8s.VolumeMount(name=_STATE_VOLUME_NAME, mount_path=_STATE_DIR),
            k8s.VolumeMount(name=_WORKSPACE_VOLUME_NAME, mount_path=_WORKSPACE_DIR),
            k8s.VolumeMount(name=_RUNNER_VOLUME_NAME, mount_path=_RUNNER_DIR, read_only=True),
            *self.trust_mounts,
        ]

    def _isolated_executor_mounts(self) -> list[k8s.VolumeMount]:
        return [
            k8s.VolumeMount(name=_HOME_VOLUME_NAME, mount_path=_HOME_DIR),
            k8s.VolumeMount(name=_TMP_VOLUME_NAME, mount_path=_TMP_DIR),
            k8s.VolumeMount(name=_EXECUTOR_VOLUME_NAME, mount_path=_EXECUTOR_DIR),
        ]

    def _executor_health_probe(self) -> k8s.Probe:
        return k8s.Probe(
            exec=k8s.ExecAction(
                command=[f"{_RUNNER_DIR}/bin/agentplane-runner", "--executor-container-health", _EXECUTOR_SOCKET]
            ),
            initial_delay_seconds=5,
            period_seconds=5,
            timeout_seconds=2,
            failure_threshold=1,
        )

    def _executor_container(self) -> k8s.Container:
        return k8s.Container(
            name=_EXECUTOR_CONTAINER_NAME,
            image=f"{self.image}:{_PLACEHOLDER_TAG}",
            command=[f"{_RUNNER_DIR}/bin/agentplane-runner"],
            args=[
                "--executor-container",
                "--socket",
                _EXECUTOR_SOCKET,
                "--listen",
                _EXECUTOR_URL,
                "--binary",
                "/bin/codex",
            ],
            working_dir=_STATE_DIR,
            security_context=sandbox_pod.workload_security_context(),
            env=sandbox_pod.workload_environment(
                self.env, [*sandbox_pod.egress_env(), *(key.env() for key in self.key_files)]
            ),
            resources=self.executor_resources
            or k8s.ResourceRequirements(
                requests={"cpu": k8s.Quantity.from_string("250m"), "memory": k8s.Quantity.from_string("512Mi")},
                limits={"cpu": k8s.Quantity.from_string("2"), "memory": k8s.Quantity.from_string("2Gi")},
            ),
            liveness_probe=self._executor_health_probe(),
            readiness_probe=self._executor_health_probe(),
            volume_mounts=[
                *self._execution_mounts(),
                *self._isolated_executor_mounts(),
                *sandbox_pod.platform_control_mounts(),
                *(key.mount() for key in self.key_files),
            ],
        )

    def _runner_container(self) -> k8s.Container:
        # Images own PATH and inherited defaults; templates explicitly choose HOME.
        # Both Pod and VM configurations use the same explicit inheritance/override contract.
        harness_env = list(
            dict.fromkeys(
                [
                    "PATH",
                    *self.env.sandbox_workload_env,
                    *(var.name for var in sandbox_pod.egress_env()),
                    *self.agent_cli_env,
                    *(key.env_var for key in self.key_files),
                ]
            )
        )
        control = platform_routes.control(self.env.namespace, service=self.control_service)
        ingress = platform_routes.ingress(self.env.namespace)
        llm_url = llm_ingress.proxied_url(self.env.namespace)
        config = Configuration(
            version=2,
            session_reconcile_concurrency=self.session_reconcile_concurrency,
            state_dir=_STATE_DIR,
            initialization_cwd=_STATE_DIR,
            control=Endpoint(target=control.local_target, bearer=platform_routes.ROUTE_BEARER),
            ingress=Endpoint(target=ingress.local_target, bearer=platform_routes.ROUTE_BEARER),
            platform_instructions=platform_instructions(self.env, egress_api_service=self.egress_api_service),
            harness_environment=configuration.EnvironmentVariables(
                inherit=tuple(dict.fromkeys((*self.env.harness_inherited_env, *harness_env))),
                values={"HOME": _HOME_DIR},
            ),
            claude=Harness(binary="/bin/claude", base_url=llm_url, token_env="ANTHROPIC_AUTH_TOKEN"),
            codex=Harness(binary="/bin/codex", base_url=f"{llm_url}/v1", token_env="OPENAI_API_KEY"),
            environment=PodEnvironment(
                executor=ContainerExecutor(url=_EXECUTOR_URL, socket=_EXECUTOR_SOCKET)
                if self.env.isolated_codex_executor
                else None
            ),
        )
        ConfigMap(
            self,
            "runner-config",
            metadata=ApiObjectMetadata(name=self.config_map_name, namespace=self.env.namespace),
            data={"runner.json": config.model_dump_json(exclude_none=True)},
        )
        return k8s.Container(
            name="runner",
            image=f"{self.image}:{_PLACEHOLDER_TAG}",
            command=[f"{_RUNNER_DIR}/bin/agentplane-runner"],
            args=["--config", CONFIG_FILE, "--launch-file", LAUNCH_FILE],
            # The runner works in absolute paths. This is for a command exec'd in: the sandbox
            # Actions' `runner` boxes start there unless the caller names a directory.
            working_dir=_STATE_DIR,
            security_context=sandbox_pod.workload_security_context(),
            env=sandbox_pod.workload_environment(
                self.env,
                [
                    *(
                        k8s.EnvVar(name=key, value=value)
                        for key, value in telemetry.service_environment(
                            self.env.namespace, "agentplane-runner", endpoint=platform_routes.telemetry_endpoint()
                        ).items()
                    ),
                    *(key.env() for key in self.key_files),
                    # On the container and not just on the harness children the runner spawns.
                    *sandbox_pod.egress_env(),
                    *(k8s.EnvVar(name=name, value=value) for name, value in self.agent_cli_env.items()),
                    # Neither a workload token nor a LiteLLM key: the placeholder the
                    # agentplane-workload EgressCredential derives from its name. Central
                    # substitutes the sidecar-only projected token; the ingress replaces
                    # that with its server-held LiteLLM virtual key after live
                    # WorkloadPrincipal resolution.
                    k8s.EnvVar(name="ANTHROPIC_AUTH_TOKEN", value=_WORKLOAD_PLACEHOLDER),
                    k8s.EnvVar(name="OPENAI_API_KEY", value=_WORKLOAD_PLACEHOLDER),
                ],
            ),
            resources=self.runner_resources
            or k8s.ResourceRequirements(
                requests={"cpu": k8s.Quantity.from_string("500m"), "memory": k8s.Quantity.from_string("1Gi")},
                limits={"cpu": k8s.Quantity.from_string("2"), "memory": k8s.Quantity.from_string("4Gi")},
            ),
            volume_mounts=[
                *self._execution_mounts(),
                k8s.VolumeMount(name=_CONFIG_VOLUME, mount_path=CONFIG_FILE, sub_path="runner.json", read_only=True),
                *(self._isolated_executor_mounts() if self.env.isolated_codex_executor else []),
                *sandbox_pod.platform_control_mounts(),
                *(key.mount() for key in self.key_files),
            ],
        )

    def add_launcher_relay_settings(self, *, name: str) -> str:
        """The sidecar settings of a VM launcher's relay, as ConfigMap `name`: this template's own, but
        listening on every interface, because the guest is outside the Pod's loopback."""
        return self._add_sidecar_settings(name=name, listen_host="0.0.0.0").config_map.name

    def _add_sidecar_settings(self, *, name: str, listen_host: str | None = None) -> SettingsFile:
        bind_host = listen_host or "127.0.0.1"
        return sandbox_pod.add_sidecar_settings(
            self,
            self.env,
            name=name,
            platform_routes=platform_routes.sidecar_routes(
                self.env.namespace, control_service=self.control_service, listen_host=bind_host
            ),
            listen_address=(f"{bind_host}:{sandbox_pod.SIDECAR_LISTEN_PORT}" if listen_host is not None else None),
        )

    def _add_sandbox_template(self) -> dict[str, Any]:
        sidecar_settings = self._add_sidecar_settings(name=f"{self.name}-egress-sidecar")
        pod = sandbox_pod.render_pod_template(
            {**_LABELS, **telemetry.EMITTER_RUNNER_CLASS_LABELS},
            sandbox_pod.pod_spec(
                self.env,
                workload=self._runner_container(),
                sidecar_settings=sidecar_settings,
                additional_workloads=([self._executor_container()] if self.env.isolated_codex_executor else []),
                # Let LINSTOR's storage-pool topology place the strict-local HDD claims.
                workload_volumes=[
                    k8s.Volume(
                        name=_RUNNER_VOLUME_NAME,
                        image=k8s.ImageVolumeSource(reference=runner_vm.RUNNER_DISK_IMAGE, pull_policy="IfNotPresent"),
                    ),
                    k8s.Volume(name=_CONFIG_VOLUME, config_map=k8s.ConfigMapVolumeSource(name=self.config_map_name)),
                    *(
                        [
                            k8s.Volume(name=_HOME_VOLUME_NAME, empty_dir=k8s.EmptyDirVolumeSource()),
                            k8s.Volume(name=_TMP_VOLUME_NAME, empty_dir=k8s.EmptyDirVolumeSource()),
                            k8s.Volume(name=_EXECUTOR_VOLUME_NAME, empty_dir=k8s.EmptyDirVolumeSource()),
                        ]
                        if self.env.isolated_codex_executor
                        else []
                    ),
                    *(key.volume() for key in self.key_files),
                ],
                sidecar_resources=self.sidecar_resources,
                image_pull_secret_name=self.image_pull_secret_name,
                enable_telemetry=True,
            ),
        )
        state_volume = {
            "name": _STATE_VOLUME_NAME,
            "layout": {"type": "DirectoryTree"},
            "claim": {
                # The StorageClass selects Piraeus' hdd pool; WaitForFirstConsumer lets LINSTOR's
                # topology-aware provisioner place both strict-local claims on a node with that pool.
                "storageClassName": self.storage_class,
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "10Gi"}},
            },
        }
        spec = {
            "pod": pod,
            "stateVolume": state_volume,
            "workspaceVolume": workspace_volume_spec(state_volume, layout_type="DirectoryTree"),
        }
        ApiObject(
            self,
            "sandboxtemplate",
            api_version=f"{GROUP}/{VERSION}",
            kind="SandboxTemplate",
            metadata=ApiObjectMetadata(name=self.name, namespace=self.env.namespace),
        ).add_json_patch(JsonPatch.add("/spec", spec))
        return spec
