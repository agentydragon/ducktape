from __future__ import annotations

from dataclasses import dataclass

from inop.config import OptimizerConfig
from openai_utils.client_factory import build_client
from openai_utils.model import OpenAIModelProto
from openai_utils.types import ReasoningEffort


@dataclass
class OptimizerModels:
    """Adapter model instances injected into the optimizer flow.

    All models implement OpenAIModelProto and are fully constructed by the caller.
    This factory is a convenience for standard configs; higher layers should DI
    these instances rather than constructing models inside the optimizer.
    """

    pe_model: OpenAIModelProto
    runner_model: OpenAIModelProto
    grader_model: OpenAIModelProto
    summarizer_model: OpenAIModelProto


def create_optimizer_models(cfg: OptimizerConfig, *, enable_debug_logging: bool = False) -> OptimizerModels:
    """Build standard adapter model instances from OptimizerConfig.

    - pe_model, runner_model: use cfg.prompt_engineer.* fields (model + reasoning_effort)
      with the shared optimizer context window size.
    - grader_model: uses cfg.grader.*
    - summarizer_model: uses cfg.summarizer.model without reasoning effort overrides.
    """
    pe_effort = cfg.prompt_engineer.reasoning_effort
    grader_effort = cfg.grader.reasoning_effort

    pe_model = build_client(
        cfg.prompt_engineer.model,
        enable_debug_logging=enable_debug_logging,
        reasoning_effort=ReasoningEffort(pe_effort) if pe_effort else None,
    )
    runner_model = build_client(
        cfg.prompt_engineer.model,
        enable_debug_logging=enable_debug_logging,
        reasoning_effort=ReasoningEffort(pe_effort) if pe_effort else None,
    )
    grader_model = build_client(
        cfg.grader.model,
        enable_debug_logging=enable_debug_logging,
        reasoning_effort=ReasoningEffort(grader_effort) if grader_effort else None,
    )
    summarizer_model = build_client(cfg.summarizer.model, enable_debug_logging=enable_debug_logging)

    return OptimizerModels(
        pe_model=pe_model, runner_model=runner_model, grader_model=grader_model, summarizer_model=summarizer_model
    )
