#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Throughput benchmark for GRPO Wordle training.

Each probe is a subprocess of `wordle_train.py --metrics-out=<json>`. We read
the JSON back. Server-mode probes share one `trl vllm-serve` (restarted only
when --enable_prefix_caching or other vLLM flags differ between probes).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).parent
LOGDIR = Path("/tmp/wordle_bench")
VLLM_PORT = 8000
MAX_STEPS = 5
N_PROMPTS = 320  # 5 steps * 64 prompts/step (effective batch held at 64 across probes).


@dataclass(frozen=True)
class Probe:
    name: str
    suite: str
    mode: str
    train_args: list[str]
    num_generations: int
    vllm_args: list[str]


@dataclass(frozen=True)
class RunningVLLM:
    proc: subprocess.Popen
    log_path: Path
    ready_gpu_memory_mib: dict[int, int]


def _probe(
    name: str, suite: str, mode: str, train_args: list[str], num_generations: int, vllm_args: list[str] | None = None
) -> Probe:
    return Probe(
        name=name,
        suite=suite,
        mode=mode,
        train_args=train_args,
        num_generations=num_generations,
        vllm_args=vllm_args or [],
    )


# Each probe pins num_generations=8 and gradient_checkpointing=on
# explicitly: those were the wordle_train.py defaults when the data was
# collected, but the script defaults have since moved to the all_on
# config. Explicit flags here lock probes to the historical comparison
# so re-runs give the same numbers.
_ISO_DEFAULTS = ["--num-generations", "8", "--gradient-checkpointing"]

# Probes from earlier runs keep their *.metrics.json on disk; the report at the bottom
# scans every known probe and shows what's there. Keep the historical throughput suite
# separate from thinking-mode probes because the vLLM context/KV-cache tradeoff changes.
THROUGHPUT_PROBES: list[Probe] = [
    # --- first run (measured with MAX_STEPS=10; no per-step timings) ---
    # _probe("baseline", "throughput", "server", [], 8),
    # _probe("num_gen_16", "throughput", "server", ["--num-generations", "16"], 16),
    # _probe("no_grad_ckpt", "throughput", "server", ["--no-gradient-checkpointing"], 8),
    # _probe("max_compl_512", "throughput", "server", ["--max-completion-length", "512"], 8),
    # _probe("colocate", "throughput", "colocate", [], 8),
    # --- second run ---
    # bsN_gaM: pure-parallelism test, effective batch = bs*ga held at 64.
    # bs >= 16 OOMs on 32 GB at 1024-token rollouts.
    _probe("prefix_caching", "throughput", "server", _ISO_DEFAULTS, 8, ["--enable_prefix_caching", "True"]),
    _probe("bs2_ga32", "throughput", "server", [*_ISO_DEFAULTS, "--batch-size", "2", "--grad-accum", "32"], 8),
    _probe("bs4_ga16", "throughput", "server", [*_ISO_DEFAULTS, "--batch-size", "4", "--grad-accum", "16"], 8),
    _probe("bs8_ga8", "throughput", "server", [*_ISO_DEFAULTS, "--batch-size", "8", "--grad-accum", "8"], 8),
    # bs16_ga4 OOMs at ~29 GB on a 32 GB card with 1024-token rollouts. Real
    # memory wall, not in-process fallout. See throughput_results.md.
    # _probe("bs16_ga4", "throughput", "server", ["--batch-size", "16", "--grad-accum", "4"], 8),
    # All-on (bs=8 OOM'd at 31.3 GB / 32 GB with num_gen=16 + grad_ckpt off,
    # 50 MB short). Drop to bs=4 to fit; effective batch still = 64.
    _probe(
        "all_on",
        "throughput",
        "server",
        ["--batch-size", "4", "--grad-accum", "16", "--num-generations", "16", "--no-gradient-checkpointing"],
        16,
    ),
    # async_grpo dropped: trl.experimental.AsyncGRPOTrainer hard-codes fp32 model
    # load and has no PEFT support (true on trl main as of 2026-05-03), so the
    # comparison vs LoRA-bf16 baseline isn't apples-to-apples. See TODO.md.
]

THINKING_PROBES: list[Probe] = [
    # Thinking mode needs enough completion budget for <think>...</think> plus
    # the tool call. Start with conservative trainer micro-batches; the vLLM
    # server-side max_model_len is the main fit/speed knob for this suite.
    _probe(
        "think_1024_safe",
        "thinking",
        "server",
        [
            "--think",
            "--max-completion-length",
            "1024",
            "--vllm-max-model-length",
            "4096",
            "--batch-size",
            "1",
            "--grad-accum",
            "64",
            "--num-generations",
            "8",
            "--gradient-checkpointing",
        ],
        8,
        ["--max-model-len", "4096"],
    ),
    _probe(
        "think_2048_safe",
        "thinking",
        "server",
        [
            "--think",
            "--max-completion-length",
            "2048",
            "--vllm-max-model-length",
            "8192",
            "--batch-size",
            "1",
            "--grad-accum",
            "64",
            "--num-generations",
            "8",
            "--gradient-checkpointing",
        ],
        8,
        ["--max-model-len", "8192"],
    ),
    _probe(
        "think_1024_mem075",
        "thinking",
        "server",
        [
            "--think",
            "--max-completion-length",
            "1024",
            "--vllm-max-model-length",
            "4096",
            "--batch-size",
            "1",
            "--grad-accum",
            "64",
            "--num-generations",
            "8",
            "--gradient-checkpointing",
        ],
        8,
        ["--max-model-len", "4096", "--gpu-memory-utilization", "0.75"],
    ),
    _probe(
        "think_4096_mem075",
        "thinking",
        "server",
        [
            "--think",
            "--max-completion-length",
            "4096",
            "--vllm-max-model-length",
            "8192",
            "--batch-size",
            "1",
            "--grad-accum",
            "64",
            "--num-generations",
            "8",
            "--gradient-checkpointing",
        ],
        8,
        ["--max-model-len", "8192", "--gpu-memory-utilization", "0.75"],
    ),
    _probe(
        "think_8192_mem075",
        "thinking",
        "server",
        [
            "--think",
            "--max-completion-length",
            "8192",
            "--vllm-max-model-length",
            "16384",
            "--batch-size",
            "1",
            "--grad-accum",
            "64",
            "--num-generations",
            "8",
            "--gradient-checkpointing",
        ],
        8,
        ["--max-model-len", "16384", "--gpu-memory-utilization", "0.75"],
    ),
    _probe(
        "think_1024_ng16",
        "thinking",
        "server",
        [
            "--think",
            "--max-completion-length",
            "1024",
            "--vllm-max-model-length",
            "4096",
            "--batch-size",
            "1",
            "--grad-accum",
            "64",
            "--num-generations",
            "16",
            "--gradient-checkpointing",
        ],
        16,
        ["--max-model-len", "4096"],
    ),
]

HISTORICAL_PROBES: list[Probe] = [
    _probe("baseline", "throughput", "server", [], 8),
    _probe("num_gen_16", "throughput", "server", ["--num-generations", "16"], 16),
    _probe("no_grad_ckpt", "throughput", "server", ["--no-gradient-checkpointing"], 8),
    _probe("max_compl_512", "throughput", "server", ["--max-completion-length", "512"], 8),
    _probe("colocate", "throughput", "colocate", [], 8),
    _probe("bs16_ga4", "throughput", "server", ["--batch-size", "16", "--grad-accum", "4"], 8),
]

ACTIVE_PROBES = [*THROUGHPUT_PROBES, *THINKING_PROBES]
KNOWN_PROBES: dict[str, Probe] = {probe.name: probe for probe in [*HISTORICAL_PROBES, *ACTIVE_PROBES]}


def gpu_memory_mib() -> dict[int, int]:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "nvidia-smi failed")
    memory = {}
    for line in result.stdout.splitlines():
        index, used = [part.strip() for part in line.split(",", maxsplit=1)]
        memory[int(index)] = int(used)
    return memory


class GPUMemorySampler:
    def __init__(self, interval_s: float = 1.0):
        self.interval_s = interval_s
        self.peak_mib: dict[int, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._error: BaseException | None = None

    def __enter__(self):
        self._record()
        self._thread.start()
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self._stop.set()
        self._thread.join()
        self._record()
        if exc_type is None and self._error is not None:
            raise self._error

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._record()

    def _record(self) -> None:
        try:
            for index, used in gpu_memory_mib().items():
                self.peak_mib[index] = max(used, self.peak_mib.get(index, 0))
        except (OSError, RuntimeError, ValueError) as e:
            self._error = e
            self._stop.set()


def _arg_value(args: list[str], *names: str) -> str:
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
    return ""


def _metric(metrics: dict, key: str) -> object:
    return metrics.get(key, metrics.get("last_step_log", {}).get(key))


def _last_step_log_from_train_log(name: str) -> dict:
    path = LOGDIR / f"{name}.log"
    if not path.exists():
        return {}
    for line in reversed(path.read_text(errors="replace").splitlines()):
        if "completions/clipped_ratio" not in line:
            continue
        start = line.find("{")
        end = line.rfind("}")
        if start == -1 or end == -1:
            continue
        try:
            parsed = ast.literal_eval(line[start : end + 1])
        except (SyntaxError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def wait_health(timeout: float = 600.0) -> None:
    deadline = time.time() + timeout
    url = f"http://localhost:{VLLM_PORT}/health/"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(5)
    raise RuntimeError(f"vllm-serve not healthy after {timeout}s: {url}")


def start_vllm(name: str, extra_args: list[str] | None = None) -> RunningVLLM:
    extra = extra_args or []
    label = " ".join(extra) if extra else "(default args)"
    print(f"[bench] starting vllm-serve on GPU 0 {label}", flush=True)
    log_path = LOGDIR / f"{name}.vllm.log"
    log = log_path.open("w")
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--with",
        "trl[vllm]",
        "--with",
        "transformers @ git+https://github.com/huggingface/transformers.git@main",
        "trl",
        "vllm-serve",
        "--model",
        "Qwen/Qwen3-1.7B",
        *extra,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "CUDA_VISIBLE_DEVICES": "0"},
        cwd=str(REPO),
    )
    try:
        wait_health()
    except Exception:
        proc.terminate()
        raise
    ready_gpu_memory_mib = gpu_memory_mib()
    print(f"[bench] vllm-serve ready (pid={proc.pid}, gpu0={ready_gpu_memory_mib.get(0, 'n/a')} MiB)", flush=True)
    return RunningVLLM(proc=proc, log_path=log_path, ready_gpu_memory_mib=ready_gpu_memory_mib)


def stop_vllm(server: RunningVLLM | None) -> None:
    if server is None:
        return
    print("[bench] stopping vllm-serve", flush=True)
    proc = server.proc
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    subprocess.run(["pkill", "-f", "VLLM::EngineCore"], check=False)
    time.sleep(2)


def run_probe(probe: Probe, max_steps: int, n_prompts: int, server: RunningVLLM | None) -> dict | None:
    cuda = "0" if probe.mode == "colocate" else "1"
    metrics_path = LOGDIR / f"{probe.name}.metrics.json"
    metrics_path.unlink(missing_ok=True)
    log_path = LOGDIR / f"{probe.name}.log"
    cmd = ["uv", "run", "wordle_train.py"]
    if probe.mode == "colocate":
        cmd.append("--colocate")
    cmd += ["--max-steps", str(max_steps), "--n-prompts", str(n_prompts), "--metrics-out", str(metrics_path)]
    cmd += probe.train_args
    config = {
        "name": probe.name,
        "suite": probe.suite,
        "mode": probe.mode,
        "train_cmd": cmd,
        "vllm_args": probe.vllm_args,
        "vllm_log": str(server.log_path) if server is not None else None,
        "vllm_ready_gpu_memory_mib": server.ready_gpu_memory_mib if server is not None else {},
    }
    (LOGDIR / f"{probe.name}.config.json").write_text(json.dumps(config, indent=2))
    print(f"[bench] probe: {probe.name} ({probe.mode}) :: {' '.join(shlex.quote(s) for s in cmd)}", flush=True)
    started = time.time()
    with log_path.open("w") as log, GPUMemorySampler() as sampler:
        rc = subprocess.run(
            cmd,
            check=False,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "CUDA_VISIBLE_DEVICES": cuda},
            cwd=str(REPO),
        ).returncode
    elapsed = time.time() - started
    print(f"  rc={rc} elapsed={elapsed:.0f}s peak_mem={sampler.peak_mib}", flush=True)
    if rc != 0 or not metrics_path.exists():
        (LOGDIR / f"{probe.name}.failure.json").write_text(
            json.dumps(
                {**config, "returncode": rc, "elapsed_s": elapsed, "gpu_memory_peak_mib": sampler.peak_mib}, indent=2
            )
        )
        print(f"  FAILED, see {log_path}", flush=True)
        return None
    metrics = json.loads(metrics_path.read_text())
    metrics.update({"bench_elapsed_s": elapsed, "gpu_memory_peak_mib": sampler.peak_mib, "bench_config": config})
    metrics_path.write_text(json.dumps(metrics, indent=2))
    return metrics


def fmt(x: object) -> str:
    if isinstance(x, float):
        return f"{x:.3f}"
    return "MISSING" if x is None else str(x)


def print_table(rows: list[list[str]]) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    sep = "  "
    for i, r in enumerate(rows):
        print(sep.join(c.ljust(w) for c, w in zip(r, widths, strict=True)))
        if i == 0:
            print(sep.join("-" * w for w in widths))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["throughput", "thinking", "all"], default="throughput")
    parser.add_argument("--probes", default="", help="Comma-separated probe names to run (default: selected suite)")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument(
        "--n-prompts",
        type=int,
        default=0,
        help="Prompt rows to feed the trainer. Default: max_steps * 64, matching the historical effective batch.",
    )
    parser.add_argument(
        "--report-only", action="store_true", help="Skip running; just rebuild the table from existing *.metrics.json"
    )
    args = parser.parse_args()

    LOGDIR.mkdir(parents=True, exist_ok=True)
    selected = set(args.probes.split(",")) if args.probes else None
    suite_probes = (
        ACTIVE_PROBES if args.suite == "all" else [probe for probe in ACTIVE_PROBES if probe.suite == args.suite]
    )
    to_run = [probe for probe in suite_probes if selected is None or probe.name in selected]
    n_prompts = args.n_prompts or args.max_steps * 64
    vllm_proc: RunningVLLM | None = None
    current_vllm_args: list[str] | None = None
    if not args.report_only:
        try:
            for probe in to_run:
                if probe.mode == "server":
                    if vllm_proc is None or probe.vllm_args != current_vllm_args:
                        if vllm_proc is not None:
                            stop_vllm(vllm_proc)
                            time.sleep(5)
                        vllm_proc = start_vllm(probe.name, probe.vllm_args)
                        current_vllm_args = probe.vllm_args
                elif vllm_proc is not None:
                    stop_vllm(vllm_proc)
                    vllm_proc = None
                    current_vllm_args = None
                    time.sleep(5)
                run_probe(probe, args.max_steps, n_prompts, vllm_proc)
        finally:
            stop_vllm(vllm_proc)

    headers = [
        "probe",
        "suite",
        "mode",
        "ctx",
        "compl",
        "runtime_s",
        "mean_len",
        "max_len",
        "term_len",
        "ss_step_s",
        "ss_compl/s",
        "raw_compl/s",
        "clip",
        "tool_call",
        "reward",
        "gpu0_peak",
        "gpu1_peak",
    ]
    rows = [headers]
    for name, probe in KNOWN_PROBES.items():
        path = LOGDIR / f"{name}.metrics.json"
        if not path.exists():
            rows.append(
                [
                    name,
                    probe.suite,
                    probe.mode,
                    _arg_value(probe.vllm_args, "--max-model-len", "--max_model_len"),
                    _arg_value(probe.train_args, "--max-completion-length"),
                    "MISSING",
                    "MISSING",
                    "MISSING",
                    "MISSING",
                    "MISSING",
                    "MISSING",
                    "MISSING",
                    "MISSING",
                    "MISSING",
                    "MISSING",
                    "MISSING",
                    "MISSING",
                ]
            )
            continue
        m = json.loads(path.read_text())
        if "last_step_log" not in m:
            m["last_step_log"] = _last_step_log_from_train_log(name)
        runtime = m.get("train_runtime")
        ss_step = m.get("steady_state_step_time_mean")
        ss_compl = (64 * probe.num_generations) / ss_step if isinstance(ss_step, (int, float)) and ss_step > 0 else None
        raw_compl = m.get("train_samples_per_second")
        raw_compl = raw_compl * probe.num_generations if isinstance(raw_compl, (int, float)) else None
        peak = m.get("gpu_memory_peak_mib", {})
        rows.append(
            [
                name,
                probe.suite,
                probe.mode,
                _arg_value(probe.vllm_args, "--max-model-len", "--max_model_len"),
                _arg_value(probe.train_args, "--max-completion-length"),
                fmt(runtime),
                fmt(_metric(m, "completions/mean_length")),
                fmt(_metric(m, "completions/max_length")),
                fmt(_metric(m, "completions/mean_terminated_length")),
                fmt(ss_step),
                fmt(ss_compl),
                fmt(raw_compl),
                fmt(_metric(m, "completions/clipped_ratio")),
                fmt(_metric(m, "tools/call_frequency")),
                fmt(_metric(m, "rewards/reward_func/mean")),
                fmt(peak.get("0") or peak.get(0)),
                fmt(peak.get("1") or peak.get(1)),
            ]
        )

    print()
    print_table(rows)
    csv = LOGDIR / "results.csv"
    with csv.open("w") as f:
        for r in rows:
            f.write(",".join(r) + "\n")
    print(f"\nResults written to {csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
