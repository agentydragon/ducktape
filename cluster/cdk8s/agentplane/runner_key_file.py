"""A secret file made available to a runner harness."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyFile:
    """One Secret and the file path and environment variable its runner harness uses.

    Pod and VM runner templates pass the same public reference to the key-file sync process. The
    Secret value stays out of template configuration and the egress sidecar.
    """

    secret_name: str
    mount_path: str
    file_name: str
    env_var: str

    @property
    def path(self) -> str:
        return f"{self.mount_path}/{self.file_name}"

    def runtime_config(self, namespace: str) -> dict[str, str]:
        """The public Secret reference and destination consumed by the shared sync process."""
        return {
            "namespace": namespace,
            "secret_name": self.secret_name,
            "secret_key": self.file_name,
            "mount_path": self.mount_path,
            "file_name": self.file_name,
        }
