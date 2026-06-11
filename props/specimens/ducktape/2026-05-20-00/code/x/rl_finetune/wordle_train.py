#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "torch",
#     "trl[vllm]",
#     "transformers @ git+https://github.com/huggingface/transformers.git@main",
#     "datasets",
#     "accelerate",
#     "tensorboard",
#     "nltk",
#     "peft",
#     "liger-kernel",
# ]
# ///
"""GRPO training on Wordle via TRL's environment_factory.

Self-contained Wordle implementation using NLTK word lists.
No external game server needed.

# TODO: could also use OpenEnv/TextArena's hosted Wordle environment instead
# of the in-process implementation, via their WebSocket client or Docker image.
# See https://huggingface.co/docs/trl/main/en/openenv for the integration.

Launch with two GPUs (server mode):

    # Terminal 1: vLLM inference server on GPU 0
    CUDA_VISIBLE_DEVICES=0 trl vllm-serve --model Qwen/Qwen3-1.7B

    # Terminal 2: GRPO training on GPU 1
    CUDA_VISIBLE_DEVICES=1 uv run wordle_train.py

Or single-GPU colocate mode (slower but simpler):

    uv run wordle_train.py --colocate
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from datasets import Dataset
from peft import LoraConfig
from transformers import TrainerCallback
from trl import GRPOConfig, GRPOTrainer
from trl.experimental.async_grpo import AsyncGRPOConfig, AsyncGRPOTrainer
from wordle_env import MAX_GUESSES, METRIC_FUNCS, SYSTEM_PROMPT, WordleEnv, reward_func

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Quiet down noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

MODEL = "Qwen/Qwen3-1.7B"
N_PROMPTS = 512


class StepTimer(TrainerCallback):
    """Records per-step wall-clock; lets the bench skip warmup step in averages."""

    def __init__(self):
        self.step_times: list[float] = []
        self.logs: list[dict] = []
        self._t = 0.0

    def on_step_begin(self, args, state, control, **_kwargs):
        self._t = time.perf_counter()

    def on_step_end(self, args, state, control, **_kwargs):
        self.step_times.append(time.perf_counter() - self._t)

    def on_log(self, args, state, control, logs: dict | None = None, **_kwargs):
        if logs:
            self.logs.append(dict(logs))


class WordleGRPOTrainer(GRPOTrainer):
    """Relabel WordleEnv metric reward funcs out of `train/rewards/metric_*`
    into `train/env/*` in tensorboard.

    TRL has no first-class metric channel; reward_funcs is the only per-rollout
    hook, so we use it (with reward_weights = [1, 0, ...] to keep the metric_*
    funcs out of the advantage). But they aren't actually rewards — calling them
    that in the log namespace is misleading. Relabel here so the dashboard
    reads honestly.
    """

    _PREFIX = "rewards/metric_"

    def log(self, logs: dict, start_time: float | None = None) -> None:
        relabeled = {}
        for k, v in logs.items():
            if k.startswith(self._PREFIX):
                # rewards/metric_invalid_length/mean -> env/invalid_length/mean
                relabeled["env/" + k[len(self._PREFIX) :]] = v
            else:
                relabeled[k] = v
        super().log(relabeled, start_time)


DEFAULT_LORA = LoraConfig(
    r=16,
    lora_alpha=32,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    lora_dropout=0.05,
    task_type="CAUSAL_LM",
)


def build_dataset(n_prompts: int) -> Dataset:
    return Dataset.from_dict(
        {"prompt": [[{"role": "user", "content": SYSTEM_PROMPT}]] * n_prompts, "seed": list(range(n_prompts))}
    )


def train_session(
    common_kwargs: dict,
    *,
    model,
    peft_config: LoraConfig | None = DEFAULT_LORA,
    n_prompts: int = N_PROMPTS,
    async_grpo: bool = False,
    no_vllm: bool = False,
    colocate: bool = False,
    metrics_out: str | None = None,
) -> dict:
    """One training run. `model` may be a HF id (string) or a pre-loaded (and optionally
    pre-PEFT-wrapped) model; `peft_config=None` skips PEFT wrapping (use when model is
    already wrapped, e.g. when sharing a base across bench probes)."""
    if async_grpo and (colocate or no_vllm):
        raise ValueError("async_grpo is server-mode only")

    if async_grpo:
        config = AsyncGRPOConfig(**common_kwargs)
        trainer_cls = AsyncGRPOTrainer
    else:
        config = GRPOConfig(**common_kwargs, use_vllm=not no_vllm, vllm_mode="colocate" if colocate else "server")
        trainer_cls = WordleGRPOTrainer

    step_timer = StepTimer()
    # Diagnostic side-metrics: TRL has no first-class metric channel, so we
    # piggyback on reward_funcs (the only per-rollout hook). reward_weights
    # zeroes them out of the GRPO advantage, and WordleGRPOTrainer.log moves
    # them from `train/rewards/metric_*` into `train/env/*` in tensorboard.
    # AsyncGRPOConfig doesn't carry reward_weights in this trl version, so
    # for async_grpo we keep the single reward_func.
    if async_grpo:
        reward_funcs: list = [reward_func]
    else:
        reward_funcs = [reward_func, *METRIC_FUNCS]
        config.reward_weights = [1.0] + [0.0] * len(METRIC_FUNCS)
    trainer_kwargs: dict = {
        "model": model,
        "reward_funcs": reward_funcs,
        "train_dataset": build_dataset(n_prompts),
        "args": config,
        "environment_factory": WordleEnv,
        "callbacks": [step_timer],
    }
    # AsyncGRPOTrainer doesn't (yet) accept peft_config and hard-codes the model
    # load to fp32 full-fine-tune — so the comparison vs LoRA probes isn't apples-to-apples.
    if not async_grpo:
        trainer_kwargs["peft_config"] = peft_config
    trainer = trainer_cls(**trainer_kwargs)
    try:
        train_result = trainer.train()
        metrics = dict(train_result.metrics)
        metrics["step_times"] = step_timer.step_times
        if step_log := next((log for log in reversed(step_timer.logs) if "completions/clipped_ratio" in log), None):
            metrics["last_step_log"] = step_log
            for key in [
                "num_tokens",
                "completions/mean_length",
                "completions/min_length",
                "completions/max_length",
                "completions/clipped_ratio",
                "completions/mean_terminated_length",
                "completions/min_terminated_length",
                "completions/max_terminated_length",
                "tools/call_frequency",
                "tools/failure_frequency",
                "rewards/reward_func/mean",
                "rewards/metric_invalid_length/mean",
                "rewards/metric_invalid_word/mean",
                "rewards/metric_unique_guesses/mean",
                "rewards/metric_win/mean",
            ]:
                if key in step_log:
                    metrics[key] = step_log[key]
        steady = step_timer.step_times[1:]
        if steady:
            metrics["steady_state_step_time_mean"] = sum(steady) / len(steady)
            metrics["steady_state_step_time_min"] = min(steady)
            metrics["steady_state_step_time_max"] = max(steady)
        if metrics_out:
            Path(metrics_out).write_text(json.dumps(metrics, indent=2))
            logger.info("Wrote metrics to %s", metrics_out)
        return metrics
    finally:
        # Hand the next caller a clean process state. Documented APIs:
        #   - VLLMClient.close_communicator (else vllm-serve rejects the next init)
        #   - Accelerator.free_memory (releases optimizer/scheduler refs, resets step)
        #   - Accelerator.end_training (destroys torch.distributed process group)
        try:
            client = getattr(getattr(trainer, "vllm_generation", None), "vllm_client", None)
            if client is not None:
                client.close_communicator()
        except Exception as e:
            logger.warning("vllm close_communicator failed: %s", e)
        try:
            trainer.accelerator.free_memory()
            trainer.accelerator.end_training()
        except Exception as e:
            logger.warning("accelerator cleanup failed: %s", e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--colocate", action="store_true", help="Single-GPU colocate mode")
    parser.add_argument("--no-vllm", action="store_true", help="Use HF generate instead of vLLM")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--think", action="store_true", help="Enable Qwen3 thinking mode")
    parser.add_argument("--max-completion-length", type=int, default=1024)
    parser.add_argument(
        "--vllm-max-model-length",
        type=int,
        default=None,
        help="vLLM context length for colocate mode and trainer-side validation. In server mode, also pass "
        "the matching --max-model-len to `trl vllm-serve`.",
    )
    parser.add_argument("--vllm-server-port", type=int, default=8000)
    parser.add_argument("--vllm-server-timeout", type=float, default=240.0)
    # Defaults below sit at effective batch=64 with ~7x baseline throughput.
    # bs=4 measured ~9.6x in the 5-step bench but OOMs on long runs once a
    # batch with longer-than-typical rollouts pushes activations + intermediate
    # tensors above the 32 GB ceiling. bs=2 has the headroom to absorb that.
    parser.add_argument("--batch-size", type=int, default=2, help="per_device_train_batch_size")
    parser.add_argument("--grad-accum", type=int, default=32, help="gradient_accumulation_steps")
    parser.add_argument("--num-generations", type=int, default=16)
    parser.add_argument("--lr", type=float, default=5e-6)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--top-k", type=int, default=0)
    parser.add_argument("--min-p", type=float, default=0.0)
    parser.add_argument("--epochs", type=int, default=1000)
    parser.add_argument("--max-steps", type=int, default=-1, help="Cap optimizer steps; -1 = use --epochs")
    parser.add_argument("--n-prompts", type=int, default=N_PROMPTS)
    parser.add_argument(
        "--gradient-checkpointing",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Recompute activations during bwd to save memory; ~25%% slower. "
        "Off by default since the parallelism config above leaves headroom.",
    )
    parser.add_argument(
        "--liger-kernel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fused chunked linear+log_softmax+GRPO loss (avoids materializing the "
        "[bs*num_gen, seq, vocab] logits tensor). Required to fit the all-on config "
        "on a 32 GB card; without it the logits + their grad alone are ~38 GB.",
    )
    parser.add_argument("--metrics-out", type=str, default=None, help="Write train_result.metrics JSON here")
    parser.add_argument(
        "--num-completions-to-print",
        type=int,
        default=4,
        help="Rollouts shown in the per-step rich table; 0 = all (huge)",
    )
    parser.add_argument(
        "--async-grpo", action="store_true", help="Use experimental AsyncGRPOTrainer (server mode only)"
    )
    args = parser.parse_args()

    common_kwargs = {
        "output_dir": "/tmp/wordle_grpo_output",
        "num_generations": args.num_generations,
        "max_completion_length": args.max_completion_length,
        "vllm_max_model_length": args.vllm_max_model_length,
        "vllm_server_port": args.vllm_server_port,
        "vllm_server_timeout": args.vllm_server_timeout,
        "per_device_train_batch_size": args.batch_size,
        "gradient_accumulation_steps": args.grad_accum,
        "num_train_epochs": args.epochs,
        "max_steps": args.max_steps,
        "learning_rate": args.lr,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "top_k": args.top_k,
        "min_p": args.min_p,
        "bf16": True,
        "gradient_checkpointing": args.gradient_checkpointing,
        "use_liger_kernel": args.liger_kernel,
        "chat_template_kwargs": {"enable_thinking": args.think},
        "max_tool_calling_iterations": MAX_GUESSES,
        "logging_steps": 1,
        "log_completions": True,
        "num_completions_to_print": args.num_completions_to_print or None,
        "save_strategy": "no",
        "report_to": "tensorboard",
    }
    train_session(
        common_kwargs,
        model=args.model,
        n_prompts=args.n_prompts,
        async_grpo=args.async_grpo,
        no_vllm=args.no_vllm,
        colocate=args.colocate,
        metrics_out=args.metrics_out,
    )


if __name__ == "__main__":
    main()
