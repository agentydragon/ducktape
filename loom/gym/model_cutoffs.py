"""Registry of LLMs with asserted weight-freeze / knowledge cutoffs.

A model may forecast a task only if its cutoff is on or before the task's
`as_of` — otherwise its weights may already contain the outcome (leakage). A
weights-release date is a hard upper bound on the cutoff; a vendor-claimed
training cutoff is weaker than a leakage probe, so each entry records its
provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ModelCutoff:
    model_id: str
    cutoff: date
    provenance: str


KNOWN_MODEL_CUTOFFS: dict[str, ModelCutoff] = {
    cutoff.model_id: cutoff
    for cutoff in (
        ModelCutoff(
            model_id="glm-4.5",
            cutoff=date(2024, 6, 30),
            provenance="leakage-probed in finance/augur/x/pm_reifier (knows neither the 2024H2 OpenAI rounds nor end-2025 BTC)",
        ),
        ModelCutoff(
            model_id="glm-4.5-flash",
            cutoff=date(2024, 6, 30),
            provenance="same-family training data as glm-4.5; probe inherited, not independently run",
        ),
        ModelCutoff(
            model_id="llama-2-70b",
            cutoff=date(2022, 9, 30),
            provenance="Meta-claimed pretraining cutoff Sep 2022; weights released 2023-07 (hard bound)",
        ),
        ModelCutoff(
            model_id="mistral-7b-v0.1",
            cutoff=date(2023, 9, 27),
            provenance="weights released 2023-09-27 (hard bound; claimed cutoff earlier but undocumented)",
        ),
        ModelCutoff(
            model_id="llama-3.1-70b",
            cutoff=date(2023, 12, 31),
            provenance="Meta-claimed knowledge cutoff Dec 2023; weights released 2024-07 (hard bound)",
        ),
        ModelCutoff(
            model_id="gemma-2-27b", cutoff=date(2024, 6, 27), provenance="weights released 2024-06-27 (hard bound)"
        ),
    )
}


def assert_admissible(model_id: str, as_of: date) -> ModelCutoff:
    """Return the model's cutoff entry, raising if unknown or if it post-dates `as_of`."""
    if model_id not in KNOWN_MODEL_CUTOFFS:
        raise ValueError(f"unknown model — add it to KNOWN_MODEL_CUTOFFS with provenance: {model_id=}")
    model_cutoff = KNOWN_MODEL_CUTOFFS[model_id]
    if model_cutoff.cutoff > as_of:
        raise ValueError(
            f"inadmissible: {model_id=} cutoff {model_cutoff.cutoff} is after task {as_of=}; "
            "its weights may contain the outcome"
        )
    return model_cutoff
