"""An endpoint a runner dials out to through its egress sidecar, as the manifests' settings name it."""

from pydantic import BaseModel, ConfigDict, Field


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
