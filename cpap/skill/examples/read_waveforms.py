"""Read DATALOG waveform EDF files and print signal summaries.

Requires pyedflib: pip install pyedflib

Usage:
    python3 read_waveforms.py /path/to/DATALOG/20260418/
    python3 read_waveforms.py /path/to/specific_file.edf
"""

import argparse
import sys
from pathlib import Path

import numpy as np
from pyedflib import EdfReader


def summarize_edf(path: Path) -> None:
    """Print a summary of all signals in an EDF file."""
    with EdfReader(str(path)) as reader:
        start = reader.getStartdatetime()
        duration_m = reader.getFileDuration() / 60
        print(f"\n{path.name}  ({duration_m:.1f} min, {start})")
        print(f"  {'Signal':<30s} {'Hz':>6s} {'Min':>8s} {'Mean':>8s} {'Max':>8s} {'Unit':<8s}")
        print(f"  {'-' * 70}")

        for index, header in enumerate(reader.getSignalHeaders()):
            signal = reader.readSignal(index)
            if len(signal) == 0:
                continue
            print(
                f"  {header['label']:<30s} {header['sample_frequency']:>6.0f}"
                f" {np.min(signal):>8.2f} {np.mean(signal):>8.2f} {np.max(signal):>8.2f}"
                f" {header['dimension']:<8s}"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, help="EDF file or DATALOG date directory")
    args = ap.parse_args()

    if args.path.is_dir():
        files = sorted(args.path.glob("*.EDF")) + sorted(args.path.glob("*.edf"))
        if not files:
            print(f"No EDF files found in {args.path}")
            sys.exit(1)
        for f in files:
            summarize_edf(f)
    else:
        summarize_edf(args.path)


if __name__ == "__main__":
    sys.exit(main())
