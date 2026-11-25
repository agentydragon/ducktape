"""Load and fingerprint the exact bytes used by the HumanEval+ cohort runners."""

import hashlib
import json
from pathlib import Path
from typing import Any

PROMPTS_SHA256 = "6c3741d0b8be9dd9adeb29eae100372b891cb0939484b5dcfc0b973604de8cbd"


def read_frozen_prompts(path: Path) -> bytes:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != PROMPTS_SHA256:
        raise ValueError(f"{path}: expected frozen prompts with SHA-256 {PROMPTS_SHA256}, got {digest}")
    return raw


def read_frozen_cohort(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    cohort = json.loads(raw.decode())
    if not isinstance(cohort, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return cohort, hashlib.sha256(raw).hexdigest()
