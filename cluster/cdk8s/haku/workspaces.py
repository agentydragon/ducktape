"""haku-workspaces: credential mirrors consumed by Haku workloads in haku-sandbox.

The Console sandbox template, warm pool, and image pin were retired with the Sandbox MCP lane.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMergePolicy,
)

from cluster.cdk8s import external_creds
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo import secret_copy
from cluster.cdk8s.forgejo_registry import chart as forgejo_images
from cluster.cdk8s.haku.namespace import NAMESPACE
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import (
    DataFrom,
    ExternalSecret,
    SecretStoreRef,
    remote_data,
)

NAME = "haku-workspaces"
OUTPUT_DIR = f"{GENERATED_ROOT}/haku/workspaces/app"


def _external_secrets(chart: Chart) -> None:
    # Mirror the ducktape-ci Forgejo registry pull credential into haku-sandbox so Haku-authored
    # workloads can pull private images. Reflects the source secret's
    # ready-made .dockerconfigjson verbatim and only stamps the type on top (no reassembly, no
    # hardcoded host); sources the flux-system copy (itself an ExternalSecret against the scoped
    # kubernetes-forgejo-images-secret-store -- cluster/k8s/forgejo-images/).
    #
    # Reads through the wide flux-system store rather than the scoped one, unlike the sibling
    # proxies: haku-sandbox is on the flux-system store's allowlist regardless, for
    # alloy-otlp-bearer and haku-mail-token, so switching stores would narrow nothing.
    ExternalSecret(
        chart,
        "forgejo-images-creds",
        metadata=ApiObjectMetadata(name=forgejo_images.SECRET_NAME, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster("kubernetes-flux-system-secret-store"),
        data_from=[DataFrom.from_extract(forgejo_images.SECRET_NAME)],
        template=ExternalSecretSpecTargetTemplate(
            type="kubernetes.io/dockerconfigjson", merge_policy=ExternalSecretSpecTargetTemplateMergePolicy.MERGE
        ),
    )
    # Mirror the read-only ActivityWatch bearer into haku-sandbox so Haku can query the read route
    # (activitywatch-read.allegedly.works: GET plus POST /api/0/query/, read-only by route
    # construction -- cluster/docs/activitywatch/README.md) from any runtime whose kubeconfig can
    # read this namespace, with no per-call approval. The egress-fence placeholder substitution on
    # the sandbox templates stays the path for pods behind the fence; this copy serves the runtimes
    # outside it (the Claude Code web home, hostexec-free reads) and haku-state's `haku aw` CLI.
    ExternalSecret(
        chart,
        "activitywatch-read-token",
        metadata=ApiObjectMetadata(name="activitywatch-read-token", namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster("kubernetes-activitywatch-secret-store"),
        data=[remote_data("activitywatch-read-token", "token")],
    )
    ExternalSecret(
        chart,
        "coinbase-api-credentials",
        metadata=ApiObjectMetadata(name="coinbase-api-credentials", namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data("coinbase-api-credentials", key) for key in ("api_key", "api_secret")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        target_name="haku-sandbox-coinbase-api-credentials",
    )
    # tf/gitops/haku-state's `haku` account: its git credentials, which Reflector mirrors on to
    # the other haku-state git consumers, and the pull secret for Haku's own UI image.
    reader = secret_copy.reader(chart, NAMESPACE)
    secret_copy.secret_copy(
        chart,
        "haku-forgejo-git",
        reader=reader,
        mirror_namespaces=["haku-egress-proxy", "flux-system", "agentplane-index"],
    )
    secret_copy.secret_copy(
        chart, "haku-forgejo-registry-pull", reader=reader, secret_type="kubernetes.io/dockerconfigjson"
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _external_secrets(chart)
    # Consumer-owned identity for reading approved canonical credentials.
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE)
    )
    return chart


def haku_workspaces(
    chart: Chart,
    directory: RenderedDirectory,
    haku_rbac: Kustomization,
    haku_egress_proxy: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    name = "haku-workspaces"
    return flux_kustomization(
        chart,
        name,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            # Namespace and mirrored-secret destinations
            haku_rbac,
            haku_egress_proxy,
            # ExternalSecret CRD and ESO's failurePolicy: Fail webhook
            external_secrets_operator,
        ),
        description="Haku workspace credential mirrors in haku-sandbox.",
    )
