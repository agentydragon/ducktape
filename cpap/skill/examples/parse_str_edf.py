"""Parse a ResMed STR.EDF daily summary file.

EDF parser uses numpy for the data block. Report uses pandas.

Usage:
    python3 parse_str_edf.py /path/to/STR.EDF [--days N]
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

EPOCH = pd.Timestamp("1970-01-01")


def _read_field(f, width: int, count: int, dtype: type = str) -> list:
    """Read `count` fixed-width fields from an EDF header."""
    return [dtype(f.read(width).decode().strip()) for _ in range(count)]


def _skip_field(f, width: int, count: int) -> None:
    f.read(width * count)


def read_str_edf(path: Path) -> pd.DataFrame:
    """Parse STR.EDF into a DataFrame with one row per day.

    Columns use raw EDF signal labels. Multi-sample signals (MaskOn/MaskOff)
    are dropped. The Date column is converted from epoch days to datetime.
    """
    with path.open("rb") as f:
        hdr = f.read(256)
        num_records = int(hdr[236:244].decode().strip())
        num_signals = int(hdr[252:256].decode().strip())
        header_bytes = int(hdr[184:192].decode().strip())

        f.seek(256)
        labels = _read_field(f, 16, num_signals)
        _skip_field(f, 80, num_signals)  # transducer
        _skip_field(f, 8, num_signals)  # physical dimension
        phys_min = np.array(_read_field(f, 8, num_signals, float))
        phys_max = np.array(_read_field(f, 8, num_signals, float))
        dig_min = np.array(_read_field(f, 8, num_signals, int))
        dig_max = np.array(_read_field(f, 8, num_signals, int))
        _skip_field(f, 80, num_signals)  # prefiltering
        samples_per_record = _read_field(f, 8, num_signals, int)
        _skip_field(f, 32, num_signals)  # reserved

        # Read all data records
        f.seek(header_bytes)
        raw = f.read()

    # Unpack: each record has interleaved int16 samples for each signal
    total_samples = sum(samples_per_record)
    data = np.frombuffer(raw, dtype="<i2").reshape(num_records, total_samples)

    # Extract single-sample signals into a DataFrame (skip multi-sample ones)
    columns = {}
    offset = 0
    for i, (label, n) in enumerate(zip(labels, samples_per_record, strict=True)):
        if n == 1:
            digital = data[:, offset].astype(float)
            scale = (phys_max[i] - phys_min[i]) / (dig_max[i] - dig_min[i]) if dig_max[i] != dig_min[i] else 0
            columns[label] = phys_min[i] + (digital - dig_min[i]) * scale
        offset += n

    df = pd.DataFrame(columns)
    df["Date"] = EPOCH + pd.to_timedelta(df["Date"].astype(int), unit="D")
    return df


def report(records: pd.DataFrame, days: int) -> None:
    """Print the last N nights with usage as JSON records."""
    df = records[records["Duration"] > 0].sort_values("Date").tail(days).copy()
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    print(df.to_json(orient="records", indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    df = read_str_edf(args.file)
    report(df, args.days)


if __name__ == "__main__":
    main()
