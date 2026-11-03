"""Read one complete generation of a Kubernetes projected TLS Secret."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Credentials:
    tls_cert: str
    tls_key: str
    ca_bundle: str


def read_credentials(directory: Path) -> Credentials:
    generation = directory / "..data"
    for attempt in range(3):
        try:
            root = generation.resolve(strict=True) if generation.is_symlink() else directory
            return Credentials(
                tls_cert=(root / "tls.crt").read_text(),
                tls_key=(root / "tls.key").read_text(),
                ca_bundle=(root / "ca.crt").read_text(),
            )
        except FileNotFoundError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")
