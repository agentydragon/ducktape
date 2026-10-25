"""ESO-owned bootstrap credentials and the toolbox Job that installs the admin API token."""

from cdk8s import ApiObjectMetadata, App, Chart, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ConfigMap,
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    Job,
    MemoryResources,
    PodSecurityContextProps,
    RestartPolicy,
    Role,
    RolePolicyRule,
    ServiceAccount,
    Volume,
    VolumeMount,
)

from cluster.cdk8s import api_resource, namespaces, pod_policy
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAMESPACE = "gitlab-spike"
NAME = "gitlab-spike-bootstrap"
OUTPUT_DIR = f"{GENERATED_ROOT}/gitlab-spike/bootstrap"


def namespace_chart(app: App) -> Chart:
    chart = Chart(app, "gitlab-spike-namespace", disable_resource_name_hashes=True)
    for name in (NAMESPACE, "gitlab-spike-ci", "gitlab-spike-demo"):
        namespaces.namespace(
            chart, name, name=name, vpa=Vpa.RECOMMEND, labels={"pod-security.kubernetes.io/enforce": "baseline"}
        )
    for name, key in (("gitlab-root-password", "password"), ("gitlab-bootstrap-token", "token")):
        mint_bearer_secret(chart, name, name=name, namespace=NAMESPACE, key=key)
    ConfigMap(
        chart,
        "ruby",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        data={"main.rb": get_required_path(own_repo_rlocation("cluster/cdk8s/gitlab_spike/bootstrap.rb")).read_text()},
    )
    return chart


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    account = ServiceAccount(
        chart, "account", metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE), automount_token=True
    )
    role = Role(
        chart,
        "role",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        rules=[
            RolePolicyRule(resources=[ApiResource.PODS], verbs=["get", "list", "watch"]),
            RolePolicyRule(resources=[api_resource.custom_resource("", "pods/exec")], verbs=["create"]),
        ],
    )
    role.bind(account)
    script = ConfigMap(
        chart,
        "script",
        metadata=ApiObjectMetadata(name=f"{NAME}-script", namespace=NAMESPACE),
        data={
            "bootstrap.sh": get_required_path(own_repo_rlocation("cluster/cdk8s/gitlab_spike/bootstrap.sh")).read_text()
        },
    )
    job = Job(
        chart,
        "job",
        metadata=ApiObjectMetadata(
            name=f"{NAME}-v1", namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"}
        ),
        service_account=account,
        automount_service_account_token=True,
        security_context=PodSecurityContextProps(ensure_non_root=True, user=65532, group=65532),
        restart_policy=RestartPolicy.ON_FAILURE,
        active_deadline=Duration.minutes(15),
        backoff_limit=3,
    )
    job.add_container(
        name="bootstrap",
        image="registry.gitlab.com/gitlab-org/build/cng/kubectl:v19.5.1",
        command=["/bin/sh", "/scripts/bootstrap.sh"],
        env_variables={"HOME": EnvValue.from_value("/tmp")},
        volume_mounts=[
            VolumeMount(path="/scripts", volume=Volume.from_config_map(chart, "scripts", script)),
            VolumeMount(path="/tmp", volume=Volume.from_empty_dir(chart, "tmp", name="tmp")),
        ],
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(100)),
            memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
        ),
    )
    pod_policy.harden(job)
    return chart
