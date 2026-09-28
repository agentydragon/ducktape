"""Offline exogenous-model training entry point.

Reads the public source evidence (paths are constants in `evidence_data`/`sources`), fits a
chosen model, and writes the result as YAML. `vecm` and `structural_macro` embed their fitted
state directly in that YAML; `state_space`'s is large enough to stay a separate trained-state
blob (`--out-blob`, echoed into the config as an absolute path).

`structural_macro` is fitted on its own evidence loading, not the joint `vecm`/`state_space`
window — see `structural_macro_fit.py`'s module docstring for why — so `--out-provider-config`
there names the checked-in `StructuralMacroFittedDefaults` artifact
(`calibrated/trained_structural_macro.yaml`) rather than a deployable `ProviderConfig`.

Usage:

    bb run //finance/augur/x/models:train_bin -- \\
        --model vecm \\
        --out-provider-config /path/to/exogenous_provider.yaml

    bb run //finance/augur/x/models:train_bin -- \\
        --model state_space \\
        --out-provider-config /path/to/exogenous_provider.yaml \\
        --out-blob /path/to/trained_state_space.json

    bb run //finance/augur/x/models:train_bin -- \\
        --model structural_macro \\
        --out-provider-config finance/augur/x/models/calibrated/trained_structural_macro.yaml
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from finance.augur.fit.data import load_evidence
from finance.augur.x.models.provider_config import StateSpaceProviderConfig, VecmProviderConfig
from finance.augur.x.models.state_space import write_state_space_artifact
from finance.augur.x.models.state_space_fit import fit_state_space_artifact
from finance.augur.x.models.structural_macro import StructuralMacroFittedDefaults
from finance.augur.x.models.structural_macro_fit import MacroFitWindow, fit_structural_macro_defaults
from finance.augur.x.models.vecm import VecmConfig, VecmModel
from finance.evidence.loading import evidence_dir_from_env

_SUPPORTED_MODEL_LABELS = ("vecm", "state_space", "structural_macro")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train an Augur exogenous model offline.")
    parser.add_argument(
        "--model", required=True, choices=_SUPPORTED_MODEL_LABELS, help="Which exogenous model to train."
    )
    parser.add_argument(
        "--out-provider-config",
        required=True,
        type=Path,
        help=(
            "Absolute path the fitted YAML will be written to — a deployable ProviderConfig "
            "for --model vecm/state_space, or the checked-in StructuralMacroFittedDefaults "
            "artifact for --model structural_macro."
        ),
    )
    parser.add_argument(
        "--out-blob",
        type=Path,
        help=(
            "Absolute path the per-model trained state blob will be written to, echoed into "
            "the config verbatim. Required for --model state_space; the other models embed "
            "their fitted state directly in the provider config YAML and take no blob."
        ),
    )
    parser.add_argument(
        "--private-equity-config",
        action="append",
        default=[],
        type=Path,
        help=(
            "Optional private-equity training YAML to fold into the state-space artifact. "
            "May be passed more than once. Only supported for --model state_space."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    out_provider_config = args.out_provider_config.resolve()
    out_blob: Path | None = args.out_blob.resolve() if args.out_blob is not None else None

    if args.model != "state_space" and args.private_equity_config:
        raise ValueError("--private-equity-config is only supported for --model state_space")

    fitted: VecmProviderConfig | StateSpaceProviderConfig | StructuralMacroFittedDefaults
    if args.model == "vecm":
        historical, evidence = load_evidence()
        model = VecmModel(config=VecmConfig())
        model.fit(historical)
        fitted = VecmProviderConfig(
            trained_state=model.to_trained_state(),
            latest_observations={key.wire_id: point for key, point in evidence.latest_observations.items()},
            evidence_metadata=evidence.metadata,
            current_mortgage30_rate_pct=float(evidence.current_mortgage30_rate_pct),
        )
    elif args.model == "state_space":
        if out_blob is None:
            raise ValueError("--out-blob is required for --model state_space")
        historical, evidence = load_evidence()
        artifact, conditioning = fit_state_space_artifact(
            historical,
            evidence,
            private_equity_config_paths=tuple(path.resolve() for path in args.private_equity_config),
        )
        write_state_space_artifact(out_blob, artifact)
        fitted = StateSpaceProviderConfig(
            trained_artifact_path=out_blob,
            conditioning=conditioning,
            current_mortgage30_rate_pct=float(evidence.current_mortgage30_rate_pct),
        )
    elif args.model == "structural_macro":
        # The shipped defaults stay on the 1955 FRED window until the long record is scored
        # against it (#5509); switching before measuring would swap the estimate on a hunch.
        fitted = fit_structural_macro_defaults(evidence_dir_from_env(), macro_window=MacroFitWindow.FRED_1955)
    else:
        raise AssertionError(f"unsupported model {args.model!r}")

    out_provider_config.write_text(
        yaml.safe_dump(fitted.model_dump(mode="json"), sort_keys=True, default_flow_style=False), encoding="utf-8"
    )
    print(f"wrote provider config: {out_provider_config}")
    if out_blob is not None:
        print(f"wrote trained blob:    {out_blob}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
