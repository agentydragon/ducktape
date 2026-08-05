"""Python boundary for the dense-array simulator.

`augur.sim.compiler` interns strings, inspects Pydantic scenarios, reshapes Polars
external-series tables, and emits the dense `CompiledSimulation` plan the engine
consumes. Each per-domain `*CompileOutput` arena lives in its own module under
this package paired with its `codec/<X>.py` decoder twin; this `__init__` exposes
the public surface so existing `from finance.augur.sim.compiler import …` callers keep
working.
"""

from __future__ import annotations

from finance.augur.sim.compiler.assets import SaleCompileOutput
from finance.augur.sim.compiler.deductions import MIDCompileOutput, SaltCompileOutput
from finance.augur.sim.compiler.helpers import NO_CODE, StringTable
from finance.augur.sim.compiler.lifecycle import LifecycleEventCompileOutput
from finance.augur.sim.compiler.obligations import ObligationCompileOutput
from finance.augur.sim.compiler.plan import CompiledSimulation, SlotPlan, compile_simulation
from finance.augur.sim.compiler.primary_residence import PrimaryResidenceEventCompileOutput
from finance.augur.sim.compiler.private_equity import PEIssuerCompileOutput, PEPolicyCompileOutput
from finance.augur.sim.compiler.properties import LiabilityCompileOutput, PropertyCompileOutput
from finance.augur.sim.compiler.property_cashflows import PropertyCashflowCompileOutput
from finance.augur.sim.compiler.tax import TaxCompileOutput, TaxLiabilityCompileOutput
from finance.augur.sim.compiler.transfers import TransferCompileOutput
from finance.augur.sim.enums import LifecycleKind

__all__ = [
    "NO_CODE",
    "CompiledSimulation",
    "LiabilityCompileOutput",
    "LifecycleEventCompileOutput",
    "LifecycleKind",
    "MIDCompileOutput",
    "ObligationCompileOutput",
    "PEIssuerCompileOutput",
    "PEPolicyCompileOutput",
    "PrimaryResidenceEventCompileOutput",
    "PropertyCashflowCompileOutput",
    "PropertyCompileOutput",
    "SaleCompileOutput",
    "SaltCompileOutput",
    "SlotPlan",
    "StringTable",
    "TaxCompileOutput",
    "TaxLiabilityCompileOutput",
    "TransferCompileOutput",
    "compile_simulation",
]
