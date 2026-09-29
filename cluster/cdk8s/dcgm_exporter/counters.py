"""The DCGM exporter's counter set, passed via `-f` in place of the stock default-counters.csv.

The stock set omits `DCGM_FI_DEV_XID_ERRORS`, the RTX 5090 Xid-79 "GPU has fallen off the bus"
signal this exporter exists for; this set adds it plus the fields relevant to the fall-off
investigation.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from enum import StrEnum


class MetricType(StrEnum):
    GAUGE = "gauge"
    COUNTER = "counter"


@dataclass(frozen=True)
class Counter:
    field: str
    metric_type: MetricType
    help: str


COUNTERS = (
    # Fall-off / marginal-link signals
    Counter(
        field="DCGM_FI_DEV_XID_ERRORS", metric_type=MetricType.GAUGE, help="Value of the last XID error encountered."
    ),
    Counter(
        field="DCGM_FI_DEV_PCIE_REPLAY_COUNTER", metric_type=MetricType.GAUGE, help="Total number of PCIe retries."
    ),
    # Power / thermal: fall-off context; the 2026-07-18 event was a 12W idle fall-off.
    Counter(field="DCGM_FI_DEV_POWER_USAGE", metric_type=MetricType.GAUGE, help="Power draw (in W)."),
    Counter(
        field="DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION",
        metric_type=MetricType.COUNTER,
        help="Total energy consumption since boot (in mJ).",
    ),
    Counter(field="DCGM_FI_DEV_GPU_TEMP", metric_type=MetricType.GAUGE, help="GPU temperature (in C)."),
    Counter(field="DCGM_FI_DEV_MEMORY_TEMP", metric_type=MetricType.GAUGE, help="Memory temperature (in C)."),
    Counter(
        field="DCGM_FI_DEV_THERMAL_VIOLATION",
        metric_type=MetricType.COUNTER,
        help="Throttling duration due to thermal constraints (in us).",
    ),
    Counter(
        field="DCGM_FI_DEV_POWER_VIOLATION",
        metric_type=MetricType.COUNTER,
        help="Throttling duration due to power constraints (in us).",
    ),
    # Link / clocks
    Counter(field="DCGM_FI_DEV_SM_CLOCK", metric_type=MetricType.GAUGE, help="SM clock frequency (in MHz)."),
    Counter(field="DCGM_FI_DEV_MEM_CLOCK", metric_type=MetricType.GAUGE, help="Memory clock frequency (in MHz)."),
    # Utilization / memory
    Counter(field="DCGM_FI_DEV_GPU_UTIL", metric_type=MetricType.GAUGE, help="GPU utilization (in %)."),
    Counter(field="DCGM_FI_DEV_MEM_COPY_UTIL", metric_type=MetricType.GAUGE, help="Memory utilization (in %)."),
    Counter(field="DCGM_FI_DEV_FB_FREE", metric_type=MetricType.GAUGE, help="Framebuffer memory free (in MiB)."),
    Counter(field="DCGM_FI_DEV_FB_USED", metric_type=MetricType.GAUGE, help="Framebuffer memory used (in MiB)."),
    # ECC: GeForce may not populate these; an unsupported field is harmless.
    Counter(
        field="DCGM_FI_DEV_ECC_SBE_VOL_TOTAL",
        metric_type=MetricType.COUNTER,
        help="Total single-bit volatile ECC errors.",
    ),
    Counter(
        field="DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
        metric_type=MetricType.COUNTER,
        help="Total double-bit volatile ECC errors.",
    ),
)


def render() -> str:
    """`COUNTERS` as a dcgm-exporter counters file: one `field, prometheus type, help` record per line."""
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows((c.field, c.metric_type, c.help) for c in COUNTERS)
    return out.getvalue()
