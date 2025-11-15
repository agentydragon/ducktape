"""Test Docker env - property returning Final[str]."""
from constants import DOCKER_SERVER_NAME


class DockerWiring:
    @property
    def server_name(self) -> str:
        """Return Final[str] from property - mypy should infer str, not Any."""
        return DOCKER_SERVER_NAME
