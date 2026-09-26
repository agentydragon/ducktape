#!/usr/bin/env bash
# Test different PipeWire quantum values and measure xruns.
# Saves full pw-top output per quantum to OUTDIR.

set -euo pipefail

DWELL=${1:-30}
QUANTUMS=(256 512 1024 2048 4096)
OUTDIR="/tmp/claude/pw-quantum-results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUTDIR"

echo "PipeWire quantum xrun test"
echo "Dwell time: ${DWELL}s per setting"
echo "Quantums to test: ${QUANTUMS[*]}"
echo "Output dir: $OUTDIR"
echo "---"

for q in "${QUANTUMS[@]}"; do
  echo "Setting quantum=$q..."
  pw-metadata -n settings 0 clock.force-quantum "$q" >/dev/null 2>&1
  pw-metadata -n settings 0 clock.min-quantum "$q" >/dev/null 2>&1
  sleep 1

  outfile="$OUTDIR/quantum_${q}.txt"
  echo "Recording pw-top for ${DWELL}s -> $outfile"
  timeout "$DWELL" pw-top -b >"$outfile" 2>&1 || true

  # Print summary line
  echo "=== quantum=$q summary ==="
  grep "alsa_output" "$outfile" | tail -1 || echo "(no alsa_output lines)"
  echo ""
done

echo "---"
echo "Restoring defaults (quantum=1024, min=1024)"
pw-metadata -n settings 0 clock.force-quantum 0 >/dev/null 2>&1
pw-metadata -n settings 0 clock.min-quantum 1024 >/dev/null 2>&1
echo "Results saved to: $OUTDIR"
echo "Done."
