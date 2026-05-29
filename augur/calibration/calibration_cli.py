"""CLI: calibrate an augur exogenous model against a prediction-market catalog.

Loads an exogenous provider config (any `ExogenousProviderConfig` YAML) and a
market catalog, runs `run_calibration`, and prints a CLI table or `--json`.

    bazelisk run //augur/calibration:calibration_cli -- \
        --model path/to/exogenous_provider.yaml --catalog path/to/catalog.yaml \
        --issuer openai --horizon-months 120 --rollouts 8000
    bazelisk run //augur/calibration:calibration_cli -- ... --live --json
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from pydantic import TypeAdapter

from augur.calibration.calibration import CalibrationResult, run_calibration
from augur.calibration.catalog import MarketCatalog
from augur.model.exogenous import Sampler
from augur.model.exogenous_provider_config import ExogenousProviderConfig

_PROVIDER_ADAPTER: TypeAdapter[ExogenousProviderConfig] = TypeAdapter(ExogenousProviderConfig)


def _load_model(path: Path) -> Sampler:
    """Realize a Sampler from an exogenous provider config YAML.

    Accepts either a bare provider config or one nested under an `exogenous_provider`
    key (as embedded in a full augur deployment config).
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    provider = document.get("exogenous_provider", document)
    return _PROVIDER_ADAPTER.validate_python(provider).realize_model()


def _print_table(result: CalibrationResult) -> None:
    print(f"# augur PM calibration -- issuer={result.issuer} as_of={result.as_of}")
    print(f"# rollouts={result.rollout_count} horizon={result.horizon_months}mo prices={result.price_source}")
    print("\n## CLEAN (apples-to-apples: augur models the event)")
    print(
        f"  {'slug':<46} {'kind':<16} {'deadline':<11} {'p_mkt':>6} {'p_model':>8} {'95% CI':>16} {'n':>6} {'unres':>6}"
    )
    for row in sorted(result.clean, key=lambda r: r.abs_gap if r.abs_gap is not None else -1.0, reverse=True):
        lo, hi = row.ci95
        pm = "  n/a " if row.p_model is None else f"{row.p_model:.3f}"
        total = row.n_resolved + row.unresolved
        unres = f"{100.0 * row.unresolved / total:.0f}%" if total else "-"
        print(
            f"  {row.slug:<46} {row.mapping_kind:<16} {row.resolution_deadline!s:<11} "
            f"{row.p_market:6.3f} {pm:>8} {f'[{lo:.3f},{hi:.3f}]':>16} {row.n_resolved:6d} {unres:>6}"
        )
    print("\n## SURFACED (augur lacks the concept -- reader interprets)")
    for surfaced in sorted(result.surfaced, key=lambda r: r.p_market, reverse=True):
        ctx = surfaced.augur_context
        ctx_str = f"   [augur {ctx.signal}={ctx.p_model:.3f}, {ctx.note}]" if ctx and ctx.p_model is not None else ""
        print(f"  p_mkt={surfaced.p_market:.3f}  {surfaced.mappability:<10} {surfaced.slug}{ctx_str}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True, help="exogenous provider config YAML")
    parser.add_argument("--catalog", type=Path, required=True, help="market catalog YAML")
    parser.add_argument("--issuer", required=True, help="private-equity issuer id to calibrate")
    parser.add_argument("--horizon-months", type=int, default=120)
    parser.add_argument("--rollouts", type=int, default=8000)
    parser.add_argument("--seed", type=int, default=1701)
    parser.add_argument(
        "--live", action="store_true", help="fetch current Manifold prices (else the curation snapshot)"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the CLI table")
    args = parser.parse_args()

    result = run_calibration(
        _load_model(args.model),
        MarketCatalog.from_yaml(args.catalog),
        issuer=args.issuer,
        horizon_months=args.horizon_months,
        rollout_seeds=tuple(range(args.seed, args.seed + args.rollouts)),
        live=args.live,
    )
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        _print_table(result)


if __name__ == "__main__":
    main()
