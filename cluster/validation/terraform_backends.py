"""Validation for tofu-controller Terraform backend configuration."""

from __future__ import annotations

import re

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import TerraformResource

_KUBERNETES_BACKEND_RE = re.compile(r'backend\s+"kubernetes"')


def check_terraform_backends(cluster: ParsedCluster) -> list[str]:
    """Reject Kubernetes Secret-backed tofu-controller state."""
    errors: list[str] = []
    for result in cluster.build_results:
        for resource in result.resources:
            if not isinstance(resource, TerraformResource):
                continue
            custom_config = ""
            if resource.spec.backend_config is not None:
                custom_config = resource.spec.backend_config.custom_configuration
            if _KUBERNETES_BACKEND_RE.search(custom_config):
                namespace = resource.namespace or "default"
                errors.append(
                    f'Terraform {namespace}/{resource.name} uses backend "kubernetes"; '
                    'use backend "pg" for tofu-controller state so locks and state live outside etcd'
                )
    return errors
