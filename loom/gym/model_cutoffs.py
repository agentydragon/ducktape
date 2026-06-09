"""Registry of LLMs with asserted knowledge cutoffs and weights-release dates.

Two distinct dates per model, because they answer different questions:

- `knowledge_cutoff` — when the model's world knowledge effectively ends,
  established by leakage probes or vendor claims. This is the **default**
  admissibility bound, but it is soft: post-training (RLHF/instruction data)
  can leak later events even when pretraining ended earlier.
- `weights_released` — when the weights shipped. A **hard** upper bound:
  weights frozen on date D cannot contain anything after D. Strict
  admissibility uses this.

A model may forecast a task only if its bound is on or before the task's
`as_of` — otherwise its weights may already contain the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ModelCutoff:
    model_id: str
    knowledge_cutoff: date
    weights_released: date
    provenance: str


KNOWN_MODEL_CUTOFFS: dict[str, ModelCutoff] = {
    cutoff.model_id: cutoff
    for cutoff in (
        ModelCutoff(
            model_id="glm-4.5",
            knowledge_cutoff=date(2024, 6, 30),
            weights_released=date(2025, 7, 28),
            provenance="cutoff leakage-probed in finance/augur/x/pm_reifier (knows neither the 2024H2 OpenAI "
            "rounds nor end-2025 BTC); weights released by Zhipu 2025-07",
        ),
        ModelCutoff(
            model_id="glm-4.5-flash",
            knowledge_cutoff=date(2024, 6, 30),
            weights_released=date(2025, 7, 28),
            provenance="same-family training data as glm-4.5; probe inherited, not independently run",
        ),
        ModelCutoff(
            model_id="llama-2-70b",
            knowledge_cutoff=date(2022, 9, 30),
            weights_released=date(2023, 7, 18),
            provenance="Meta-claimed pretraining cutoff Sep 2022",
        ),
        ModelCutoff(
            model_id="mistral-7b-v0.1",
            knowledge_cutoff=date(2023, 9, 27),
            weights_released=date(2023, 9, 27),
            provenance="no documented training cutoff; release date used for both bounds",
        ),
        ModelCutoff(
            model_id="llama-3.1-70b",
            knowledge_cutoff=date(2023, 12, 31),
            weights_released=date(2024, 7, 23),
            provenance="Meta-claimed knowledge cutoff Dec 2023",
        ),
        ModelCutoff(
            model_id="gemma-2-27b",
            knowledge_cutoff=date(2024, 6, 27),
            weights_released=date(2024, 6, 27),
            provenance="no documented training cutoff; release date used for both bounds",
        ),
    )
}


def assert_admissible(model_id: str, as_of: date, strict: bool = False) -> ModelCutoff:
    """Return the model's registry entry, raising if unknown or if its bound post-dates `as_of`.

    `strict=True` bounds by `weights_released` (hard guarantee) instead of the
    probed/claimed `knowledge_cutoff`.
    """
    if model_id not in KNOWN_MODEL_CUTOFFS:
        raise ValueError(f"unknown model — add it to KNOWN_MODEL_CUTOFFS with provenance: {model_id=}")
    model_cutoff = KNOWN_MODEL_CUTOFFS[model_id]
    bound = model_cutoff.weights_released if strict else model_cutoff.knowledge_cutoff
    if bound > as_of:
        raise ValueError(
            f"inadmissible: {model_id=} {'weights-release' if strict else 'knowledge-cutoff'} bound {bound} "
            f"is after task {as_of=}; its weights may contain the outcome"
        )
    return model_cutoff
