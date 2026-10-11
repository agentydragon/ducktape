"""The endpoints a Sandbox's runner dials out to (agentplane/docs/runner_channel.md § Event ingress).

Sandbox creation sets them from this service's configuration and nothing else does: one ingress for
every Sandbox and this service's hub for control, each with the egress policy that admits its host
under its own token audience. Those policies are bound at creation, never picked by a request.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from more_itertools import unique_everseen
from pydantic import BaseModel, ConfigDict, Field

from agentplane.sandbox_service.models import InventoryError

# The SandboxTemplate container the runner runs in.
RUNNER_CONTAINER = "runner"
_CONTROL_FLAGS = ("--channel-target", "--channel-bearer")
_INGRESS_FLAGS = ("--ingress-target", "--ingress-bearer")


class RunnerEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(
        min_length=1,
        description="host:port the runner dials through its egress sidecar, spelled as `egress_policy` matches the host.",
    )
    bearer: str = Field(
        min_length=1, description="Placeholder bearer the egress proxy swaps for the Pod's token for this audience."
    )
    egress_policy: str = Field(
        min_length=1, description="The EgressPolicy admitting `target` and substituting that token."
    )


class TemplateSetsRunnerEndpointsError(InventoryError):
    """The template's runner container already names an endpoint, or has no runner container to point."""


@dataclass(frozen=True)
class RunnerEndpoints:
    """What every new Sandbox's runner is started with."""

    control: RunnerEndpoint | None
    ingress: RunnerEndpoint | None

    def egress_policies(self) -> list[str]:
        return [endpoint.egress_policy for endpoint in (self.control, self.ingress) if endpoint is not None]

    def require_unreserved(self, picked: Sequence[str]) -> None:
        """Endpoint egress policies come with their endpoint; no request picks one."""
        reserved = set(self.egress_policies())
        if named := list(unique_everseen(name for name in picked if name in reserved)):
            raise ValueError(f"egress policies {named} are bound with their runner endpoint")

    def point(self, pod_template: dict[str, object]) -> dict[str, object]:
        """The template's Pod with these endpoints on its runner's command line."""
        args = [
            *(_flags(_CONTROL_FLAGS, self.control) if self.control is not None else []),
            *(_flags(_INGRESS_FLAGS, self.ingress) if self.ingress is not None else []),
        ]
        if not args:
            return pod_template
        spec = cast(dict[str, object], pod_template.get("spec", {}))
        containers = cast(list[dict[str, object]], spec.get("containers", []))
        runners = [index for index, container in enumerate(containers) if container.get("name") == RUNNER_CONTAINER]
        if not runners:
            raise TemplateSetsRunnerEndpointsError(f"template has no {RUNNER_CONTAINER!r} container")
        pointed = list(containers)
        for index in runners:
            existing = cast(list[str], containers[index].get("args", []))
            if named := [flag for flag in (*_CONTROL_FLAGS, *_INGRESS_FLAGS) if flag in existing]:
                raise TemplateSetsRunnerEndpointsError(f"template's runner already sets {named}")
            pointed[index] = {**containers[index], "args": [*existing, *args]}
        return {**pod_template, "spec": {**spec, "containers": pointed}}


def _flags(names: tuple[str, str], endpoint: RunnerEndpoint) -> list[str]:
    target, bearer = names
    return [target, endpoint.target, bearer, endpoint.bearer]
