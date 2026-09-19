"""Pod-level workload fields cdk8s_plus_34 has no typed builder for -- applied via
the `JsonPatch` escape hatch:

- `securityContext.seccompProfile`: `PodSecurityContextProps` has no `seccompProfile`
  field (only `ContainerSecurityContextProps` does).
"""

from __future__ import annotations

from cdk8s import ApiObject, JsonPatch
from cdk8s_plus_34 import Deployment, k8s

_POD_SPEC_PATH = "/spec/template/spec"
# A CronJob nests its pod template one level deeper, under the Job template it stamps out.
CRON_JOB_POD_SPEC_PATH = "/spec/jobTemplate/spec/template/spec"


def runtime_default_seccomp_patch(*, pod_spec_path: str = _POD_SPEC_PATH) -> JsonPatch:
    return JsonPatch.add(f"{pod_spec_path}/securityContext/seccompProfile", k8s.SeccompProfile(type="RuntimeDefault"))


def apply_pod_spec_patches(deployment: Deployment) -> None:
    ApiObject.of(deployment).add_json_patch(runtime_default_seccomp_patch())
