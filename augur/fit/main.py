"""Offline exogenous-model training entry point.

Reads the public source CSVs (paths are constants in `evidence_data`), fits a
chosen `Fittable` model, and writes two files: a `ProviderConfig` YAML
(the discriminated deployment config that the augur server reads at startup as
part of `Config.exogenous_provider`) and a per-model trained-state blob (e.g. an
`.npz` archive). The manifest YAML's `trained_blob` is an absolute path so
the deployment authoring it knows exactly where the blob will live at
runtime.

Usage:

    bb run //augur/fit:train -- \\
        --model vecm \\
        --out-provider-config /path/to/exogenous_provider.yaml \\
        --out-blob /path/to/trained_vecm.npz
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from augur.fit.data import load_evidence
from augur.fit.state_space import fit_state_space_artifact
from augur.model.provider_config import StateSpaceProviderConfig, VecmProviderConfig
from augur.model.state_space import write_state_space_artifact
from augur.model.vecm import VecmConfig, VecmModel

_SUPPORTED_MODEL_LABELS = ("vecm", "state_space")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train an Augur exogenous model offline.")
    parser.add_argument(
        "--model", required=True, choices=_SUPPORTED_MODEL_LABELS, help="Which exogenous model to train."
    )
    parser.add_argument(
        "--out-provider-config",
        required=True,
        type=Path,
        help="Absolute path the ProviderConfig YAML will be written to.",
    )
    parser.add_argument(
        "--out-blob",
        required=True,
        type=Path,
        help="Absolute path the per-model trained state blob will be written to. Echoed into the config verbatim.",
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
    out_blob = args.out_blob.resolve()

    historical, evidence = load_evidence()
    provider_config: VecmProviderConfig | StateSpaceProviderConfig

    if args.model == "vecm":
        if args.private_equity_config:
            raise ValueError("--private-equity-config is only supported for --model state_space")
        model = VecmModel(config=VecmConfig())
        model.fit(historical)
        model.save(out_blob)
        provider_config = VecmProviderConfig(
            trained_blob=out_blob,
            latest_observations=dict(evidence.latest_observations),
            current_mortgage30_rate_pct=float(evidence.current_mortgage30_rate_pct),
        )
    elif args.model == "state_space":
        artifact, conditioning = fit_state_space_artifact(
            historical,
            evidence,
            private_equity_config_paths=tuple(path.resolve() for path in args.private_equity_config),
        )
        write_state_space_artifact(out_blob, artifact)
        provider_config = StateSpaceProviderConfig(
            trained_artifact_path=out_blob,
            conditioning=conditioning,
            current_mortgage30_rate_pct=float(evidence.current_mortgage30_rate_pct),
        )
    else:
        raise AssertionError(f"unsupported model {args.model!r}")

    out_provider_config.write_text(
        yaml.safe_dump(provider_config.model_dump(mode="json"), sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"wrote provider config: {out_provider_config}")
    print(f"wrote trained blob:    {out_blob}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
