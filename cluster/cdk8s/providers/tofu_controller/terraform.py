"""Ergonomic wrapper for tofu-controller's `Terraform`, following cdk8s-plus's own construction
pattern: a class named after the kind, with keyword parameters mirroring `TerraformV1Alpha2Spec`'s
own fields. It wraps `TerraformV1Alpha2`, the served storage version (`test_terraform_import.py`).
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from tofu_controller.io.fluxcd.contrib.infra import (
    TerraformV1Alpha2 as _TerraformV1Alpha2,
    TerraformV1Alpha2Spec,
    TerraformV1Alpha2SpecBackendConfig,
    TerraformV1Alpha2SpecDependsOn,
    TerraformV1Alpha2SpecRunnerPodTemplate,
    TerraformV1Alpha2SpecSourceRef,
    TerraformV1Alpha2SpecStoreReadablePlan,
    TerraformV1Alpha2SpecVars,
)


class Terraform(_TerraformV1Alpha2):
    """`interval` and `source_ref` are the only fields the CRD requires. `None` leaves an optional
    field unset, so tofu-controller's own default applies."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        interval: str,
        source_ref: TerraformV1Alpha2SpecSourceRef,
        path: str | None = None,
        approve_plan: str | None = None,
        plan_only: bool | None = None,
        refresh_before_apply: bool | None = None,
        store_readable_plan: TerraformV1Alpha2SpecStoreReadablePlan | None = None,
        targets: Sequence[str] | None = None,
        service_account_name: str | None = None,
        backend_config: TerraformV1Alpha2SpecBackendConfig | None = None,
        vars: Sequence[TerraformV1Alpha2SpecVars] | None = None,
        depends_on: Sequence[TerraformV1Alpha2SpecDependsOn] | None = None,
        runner_pod_template: TerraformV1Alpha2SpecRunnerPodTemplate | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=TerraformV1Alpha2Spec(
                interval=interval,
                source_ref=source_ref,
                path=path,
                approve_plan=approve_plan,
                plan_only=plan_only,
                refresh_before_apply=refresh_before_apply,
                store_readable_plan=store_readable_plan,
                targets=list(targets) if targets is not None else None,
                service_account_name=service_account_name,
                backend_config=backend_config,
                vars=list(vars) if vars is not None else None,
                depends_on=list(depends_on) if depends_on is not None else None,
                runner_pod_template=runner_pod_template,
            ),
        )
