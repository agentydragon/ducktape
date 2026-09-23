"""Drift detection for the metal root (cluster/terraform/main). That root is applied from a
workstation by `bazel run //cluster:bootstrap`; this CR only ever plans it, so the operator
learns about reality/state divergence without the controller being able to act on it. See
cluster/k8s/infra-drift/README.md."""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import App, Chart
from tofu_controller.io.fluxcd.contrib.infra import (
    TerraformV1Alpha2,
    TerraformV1Alpha2Spec,
    TerraformV1Alpha2SpecBackendConfig,
    TerraformV1Alpha2SpecRunnerPodTemplate,
    TerraformV1Alpha2SpecRunnerPodTemplateSpec,
    TerraformV1Alpha2SpecSourceRef,
    TerraformV1Alpha2SpecSourceRefKind,
    TerraformV1Alpha2SpecStoreReadablePlan,
)

from cluster.cdk8s import terraform
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "infra-drift"
OUTPUT_DIR = "cluster/k8s/infra-drift"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    TerraformV1Alpha2(
        chart,
        "terraform",
        metadata=metadata(
            NAME,
            terraform.NAMESPACE,
            annotations={
                "description": (
                    "Plan-only drift watch on cluster/terraform/main (the metal root). Never applies;"
                    " `bazel run //cluster:bootstrap` remains the apply path."
                )
            },
        ),
        spec=TerraformV1Alpha2Spec(
            plan_only=True,
            # A plan without refresh compares config to state and would report "no
            # changes" against a reality nobody has looked at — refresh is what makes
            # drift observable (cluster/docs/lessons_learned/2026_04_25_tofu_controller_refresh_before_apply.md).
            refresh_before_apply=True,
            # Without this the pending plan says only that something changed. `human`
            # writes the diff to a ConfigMap in this namespace — README § Reading a plan.
            store_readable_plan=TerraformV1Alpha2SpecStoreReadablePlan.HUMAN,
            # Every plan takes the PG advisory lock on schema `main` — the lock
            # `bazel run //cluster:bootstrap` also needs. README § State lock contention
            # before shortening this.
            interval="6h",
            path="./cluster/terraform/main",
            source_ref=TerraformV1Alpha2SpecSourceRef(
                kind=TerraformV1Alpha2SpecSourceRefKind.GIT_REPOSITORY,
                name="infra-drift-source",
                namespace=terraform.NAMESPACE,
            ),
            service_account_name="tf-runner",
            # The OVH server resources and nothing else — the one subgraph whose only
            # dependencies are the ovh provider and two SOPS files. README § What this
            # covers has the reason for each exclusion, Proxmox included.
            targets=[
                "ovh_dedicated_server.kimsufi",
                # Zero instances while local.kimsufi_cp_servers is empty; listed so a
                # future Kimsufi control plane is watched without editing this CR.
                "ovh_dedicated_server.kimsufi_cp",
            ],
            backend_config=TerraformV1Alpha2SpecBackendConfig(
                custom_configuration=textwrap.dedent("""\
                    backend "pg" {
                      conn_str    = "postgres://tfstate@tofu-state-db-ovh-rw.tofu-state.svc:5432/tfstate?sslmode=disable"
                      schema_name = "main"
                    }
                """)
            ),
            runner_pod_template=TerraformV1Alpha2SpecRunnerPodTemplate(
                spec=TerraformV1Alpha2SpecRunnerPodTemplateSpec(
                    env=[
                        terraform.secret_env("PGPASSWORD", "tofu-state-db-credentials", "password"),
                        # Decrypts secrets/ovh-{credentials,rescue-ssh}.sops.yaml so the ovh
                        # provider can configure. This is the broad cluster age key, not a
                        # narrow one: tf-runner-role is a ClusterRole granting secret CRUD in
                        # every namespace, so any runner can already read this very Secret — a
                        # single-purpose key would be ceremony, not containment.
                        terraform.secret_env("SOPS_AGE_KEY", "sops-age-cluster-secrets", "age.agekey"),
                    ]
                )
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
