"""A case as the Rust engine takes it.

Tests that inspect or perturb an execution document get their own copy of the case's
prepared input. No separate financial encoding or rule lookup occurs here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from finance.augur.sim.testing.case import Case


def fixture_for(case: Case) -> dict[str, Any]:
    return deepcopy(case.compiled_run.execution_input)
