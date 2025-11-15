from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SECRETS_ROOT = Path("/var/run/ember/secrets")


class ProjectedSecret:
    """Helper for secrets mounted via projected volumes with optional env overrides."""

    def __init__(self, *, name: str, env_var: str | None = None, strip: bool = True) -> None:
        if not name:
            raise ValueError("ProjectedSecret requires a name")

        file_component = Path(name).name
        self._file_name = file_component
        self._env_var = env_var
        self._strip = strip

    def value(self, *, required: bool = False) -> str | None:
        """Return the current value, raising if required and missing."""

        value = self._read_raw()
        if required and not value:
            raise RuntimeError(f"{self._file_name} is not configured")
        return value

    def _read_raw(self) -> str | None:
        if self._env_var:
            raw = os.getenv(self._env_var)
            if raw is not None:
                return raw.strip() if self._strip else raw

        path = SECRETS_ROOT / self._file_name
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Failed to stat secret %s at %s: %s", self._file_name, path, exc)
            return None

        return raw.strip() if self._strip else raw
