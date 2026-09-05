"""Run the JAX simulator on the canonical benchmark scenario."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import resource
import time
from typing import Any

import jax
import numpy as np

from finance.augur.benchmark.scenario import feature_rich_case
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.compiler.plan import compile_simulation
from finance.augur.sim.engine.jax_engine import run_jax_scan
from finance.augur.sim.events import EVENT_FRAME_SPECS


def _checksum(arrays: list[np.ndarray[Any, Any]]) -> int:
    digest = hashlib.blake2b(digest_size=8)
    for array in arrays:
        contiguous = np.ascontiguousarray(array)
        digest.update(contiguous.dtype.str.encode())
        digest.update(np.asarray(contiguous.shape, dtype=np.int64).tobytes())
        digest.update(contiguous.view(np.uint8))
    return int.from_bytes(digest.digest(), "little")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollouts", type=int, default=500)
    parser.add_argument("--horizon-months", type=int, default=60)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="rollouts retained in one dense JAX output; defaults to all rollouts",
    )
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats must be positive")
    batch_size = args.batch_size or args.rollouts
    if batch_size <= 0 or args.rollouts % batch_size:
        parser.error("--batch-size must be positive and divide --rollouts exactly")
    batch_count = args.rollouts // batch_size

    case = feature_rich_case(rollout_count=batch_size, horizon_months=args.horizon_months)
    external = case.external_series
    gc.collect()

    def run():
        # Compiled inside the timed region, as the dense entry point this replaces did:
        # turning the scenario into a plan is JAX's counterpart to the fixture parse the
        # Rust driver also pays.
        plan = compile_simulation(
            case.scenario,
            rollout_count=batch_size,
            external_series=external,
            jurisdictions=case.jurisdictions,
            locations=dict(case.locations),
        )
        result = SimulationRun(plan=plan, output=run_jax_scan(plan), external_series=external)
        jax.block_until_ready(result.output.state.cash)
        # Python/JAX exposes canonical events through this lazy decode. Materialize it inside
        # the timed region so the dense benchmark includes the output contract callers see.
        event_log = result.events_log
        for spec in EVENT_FRAME_SPECS:
            event_log.frame(spec)
        return result

    cold_started = time.perf_counter()
    run()
    cold_seconds = time.perf_counter() - cold_started

    durations = []
    result = None
    for _ in range(args.repeats):
        result = None
        gc.collect()
        started = time.perf_counter()
        for _ in range(batch_count):
            result = run()
        durations.append(time.perf_counter() - started)
    assert result is not None
    sorted_durations = sorted(durations)
    median = sorted_durations[len(sorted_durations) // 2]
    output = result.output
    event_frames = [result.events_log.frame(spec) for spec in EVENT_FRAME_SPECS]
    dense_arrays = [np.asarray(value) for value in jax.tree_util.tree_leaves(output)]
    final_failed = np.asarray(output.state.failed[-1], dtype=np.bool_)
    report = {
        "output_mode": "dense",
        "rollout_count": args.rollouts,
        "batch_size": batch_size,
        "batch_count": batch_count,
        "horizon_months": args.horizon_months,
        "repeats": args.repeats,
        "cold_wall_seconds": cold_seconds,
        "wall_seconds": durations,
        "median_wall_seconds": median,
        "rollouts_per_second": args.rollouts / median,
        "rollout_months_per_second": args.rollouts * args.horizon_months / median,
        "logical_cpu_count": os.cpu_count(),
        "peak_self_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "jax_backend": jax.default_backend(),
        "jax_devices": [str(device) for device in jax.devices()],
        "dense_output_array_count_per_batch": len(dense_arrays),
        "dense_output_elements_per_batch": sum(array.size for array in dense_arrays),
        "dense_output_bytes_per_batch": sum(array.nbytes for array in dense_arrays),
        "canonical_event_rows_per_batch": sum(frame.height for frame in event_frames),
        "canonical_event_bytes_per_batch": sum(frame.estimated_size() for frame in event_frames),
        "cash_state_elements_per_batch": int(output.state.cash.size),
        "lot_state_elements_per_batch": int(output.state.lots.size),
        "obligation_elements_per_batch": int(output.obligations.due.size),
        "failure_count": int(final_failed.sum()) * batch_count,
        # Hash every dense state and event-source array, not just terminal cash. This is an
        # anti-dead-code checksum; semantic equality remains the differential suite's job.
        "batch_checksum": _checksum(dense_arrays),
    }
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
