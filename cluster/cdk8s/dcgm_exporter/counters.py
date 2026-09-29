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

    @classmethod
    def gauge(cls, field: str, help: str) -> Counter:
        return cls(field=field, metric_type=MetricType.GAUGE, help=help)

    @classmethod
    def counter(cls, field: str, help: str) -> Counter:
        return cls(field=field, metric_type=MetricType.COUNTER, help=help)


COUNTERS = (
    # Fall-off / marginal-link signals
    Counter.gauge(field="DCGM_FI_DEV_XID_ERRORS", help="Value of the last XID error encountered."),
    Counter.gauge(field="DCGM_FI_DEV_PCIE_REPLAY_COUNTER", help="Total number of PCIe retries."),
    # Power / thermal: fall-off context; the 2026-07-18 event was a 12W idle fall-off.
    Counter.gauge(field="DCGM_FI_DEV_POWER_USAGE", help="Power draw (in W)."),
    Counter.counter(field="DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", help="Total energy consumption since boot (in mJ)."),
    Counter.gauge(field="DCGM_FI_DEV_GPU_TEMP", help="GPU temperature (in C)."),
    Counter.gauge(field="DCGM_FI_DEV_MEMORY_TEMP", help="Memory temperature (in C)."),
    Counter.counter(
        field="DCGM_FI_DEV_THERMAL_VIOLATION", help="Throttling duration due to thermal constraints (in us)."
    ),
    Counter.counter(field="DCGM_FI_DEV_POWER_VIOLATION", help="Throttling duration due to power constraints (in us)."),
    # Link / clocks
    Counter.gauge(field="DCGM_FI_DEV_SM_CLOCK", help="SM clock frequency (in MHz)."),
    Counter.gauge(field="DCGM_FI_DEV_MEM_CLOCK", help="Memory clock frequency (in MHz)."),
    # Utilization / memory
    Counter.gauge(field="DCGM_FI_DEV_GPU_UTIL", help="GPU utilization (in %)."),
    Counter.gauge(field="DCGM_FI_DEV_MEM_COPY_UTIL", help="Memory utilization (in %)."),
    Counter.gauge(field="DCGM_FI_DEV_FB_FREE", help="Framebuffer memory free (in MiB)."),
    Counter.gauge(field="DCGM_FI_DEV_FB_USED", help="Framebuffer memory used (in MiB)."),
    # ECC: GeForce may not populate these; an unsupported field is harmless.
    Counter.counter(field="DCGM_FI_DEV_ECC_SBE_VOL_TOTAL", help="Total single-bit volatile ECC errors."),
    Counter.counter(field="DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", help="Total double-bit volatile ECC errors."),
)


def render() -> str:
    """`COUNTERS` as a dcgm-exporter counters file: one `field, prometheus type, help` record per line."""
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows((c.field, c.metric_type, c.help) for c in COUNTERS)
    return out.getvalue()
