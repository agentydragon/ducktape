"""Names and protected labels for one disposable shared-cluster test run."""

from dataclasses import dataclass
from uuid import UUID

PREFIX = "ap-test-"
TENANT_LABEL = "agentplane-test-cluster.allegedly.works/tenant"
TENANT_VALUE = "true"


@dataclass(frozen=True)
class Namespaces:
    run_id: UUID

    @property
    def application(self) -> str:
        return f"{PREFIX}{self.run_id.hex}"

    @property
    def credentials(self) -> str:
        return f"{self.application}-credentials"

    @property
    def monitoring(self) -> str:
        return f"{self.application}-monitoring"

    @property
    def all(self) -> tuple[str, str, str]:
        return self.application, self.credentials, self.monitoring
