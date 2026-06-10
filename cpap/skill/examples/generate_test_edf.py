"""Generate a synthetic ResMed-style STR.EDF for testing.

Creates a valid EDF file with CPAP-specific signals (Date, Duration, AHI, etc.)
populated with known values for deterministic test assertions.
"""

import struct
from datetime import datetime, timedelta
from pathlib import Path

EPOCH = datetime(1970, 1, 1)

# Signals in a minimal STR.EDF-like file.
# Each tuple: (label, unit, phys_min, phys_max, dig_min, dig_max, samples_per_record)
# Date uses 0..24836 digital range (1:1 with physical) to avoid quantization error.
# Other signals use the full int16 range for maximum resolution.
SIGNALS = [
    ("Date", "", 0.0, 24836.0, 0, 24836, 1),
    ("Duration", "min.", 0.0, 1440.0, -32768, 32767, 1),
    ("AHI", "", 0.0, 240.0, -32768, 32767, 1),
    ("HI", "", 0.0, 240.0, -32768, 32767, 1),
    ("OAI", "", 0.0, 240.0, -32768, 32767, 1),
    ("CAI", "", 0.0, 240.0, -32768, 32767, 1),
    ("MaskPress.50", "cmH2O", 0.0, 40.0, -32768, 32767, 1),
    ("MaskPress.95", "cmH2O", 0.0, 40.0, -32768, 32767, 1),
    ("Leak.50", "L/s", 0.0, 2.0, -32768, 32767, 1),
    ("Leak.95", "L/s", 0.0, 2.0, -32768, 32767, 1),
    ("RespRate.50", "bpm", 0.0, 90.0, -32768, 32767, 1),
    ("TidVol.50", "L", 0.0, 4.0, -32768, 32767, 1),
    ("SpO2.50", "%", 0.0, 100.0, -32768, 32767, 1),
    ("MaskOn", "MINUTES", 0.0, 1440.0, -32768, 32767, 20),
    ("MaskOff", "MINUTES", 0.0, 1440.0, -32768, 32767, 20),
]


def _to_digital(value: float, phys_min: float, phys_max: float, dig_min: int, dig_max: int) -> int:
    """Convert a physical value to its digital representation."""
    if phys_max == phys_min:
        return 0
    ratio = (value - phys_min) / (phys_max - phys_min)
    digital = dig_min + ratio * (dig_max - dig_min)
    return max(dig_min, min(dig_max, round(digital)))


def generate_str_edf(path: Path, days: list[dict[str, float]], start_date: datetime = datetime(2026, 4, 1)) -> None:
    """Write a synthetic STR.EDF file.

    Each entry in `days` is a dict mapping signal label to physical value.
    Missing signals default to 0. The `Date` signal is auto-computed from
    start_date + index.
    """
    num_signals = len(SIGNALS)
    num_records = len(days)
    header_bytes = 256 + num_signals * 256
    record_duration = 86400  # 1 day per record

    # Main header (256 bytes)
    hdr = bytearray(256)
    hdr[0:8] = f"{'0':<8s}".encode()
    hdr[8:88] = f"{'X X X X':<80s}".encode()
    hdr[88:168] = f"{'Startdate 01-APR-2026 X X test_cpap':<80s}".encode()
    hdr[168:176] = f"{'01.04.26':<8s}".encode()
    hdr[176:184] = f"{'12.00.00':<8s}".encode()
    hdr[184:192] = f"{header_bytes:<8d}".encode()
    hdr[192:236] = f"{'':<44s}".encode()
    hdr[236:244] = f"{num_records:<8d}".encode()
    hdr[244:252] = f"{record_duration:<8d}".encode()
    hdr[252:256] = f"{num_signals:<4d}".encode()

    # Signal headers (num_signals * 256 bytes total, field by field)
    def pad(s: str, n: int) -> bytes:
        return f"{s:<{n}s}".encode()[:n]

    sig_labels = b"".join(pad(s[0], 16) for s in SIGNALS)
    sig_transducer = b"".join(pad("", 80) for _ in SIGNALS)
    sig_dim = b"".join(pad(s[1], 8) for s in SIGNALS)
    sig_pmin = b"".join(pad(str(s[2]), 8) for s in SIGNALS)
    sig_pmax = b"".join(pad(str(s[3]), 8) for s in SIGNALS)
    sig_dmin = b"".join(pad(str(s[4]), 8) for s in SIGNALS)
    sig_dmax = b"".join(pad(str(s[5]), 8) for s in SIGNALS)
    sig_prefilt = b"".join(pad("", 80) for _ in SIGNALS)
    sig_nsamples = b"".join(pad(str(s[6]), 8) for s in SIGNALS)
    sig_reserved = b"".join(pad("", 32) for _ in SIGNALS)

    with path.open("wb") as f:
        f.write(bytes(hdr))
        f.write(sig_labels + sig_transducer + sig_dim + sig_pmin + sig_pmax)
        f.write(sig_dmin + sig_dmax + sig_prefilt + sig_nsamples + sig_reserved)

        for i, day in enumerate(days):
            date_val = (start_date + timedelta(days=i) - EPOCH).days
            for label, _unit, pmin, pmax, dmin, dmax, nsamples in SIGNALS:
                if label == "Date":
                    vals = [_to_digital(date_val, pmin, pmax, dmin, dmax)]
                else:
                    phys = day.get(label, 0.0)
                    if nsamples == 1:
                        vals = [_to_digital(phys, pmin, pmax, dmin, dmax)]
                    else:
                        vals = [_to_digital(phys, pmin, pmax, dmin, dmax)] + [
                            _to_digital(0, pmin, pmax, dmin, dmax)
                        ] * (nsamples - 1)
                f.write(struct.pack(f"<{len(vals)}h", *vals))
