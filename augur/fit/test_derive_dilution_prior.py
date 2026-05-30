"""Tests for the M2.2-A dilution-prior config-block renderer.

These guard that `_format_config_block` emits its `key: value` config lines
through `yaml.safe_dump` (genuine YAML floats) rather than hand-spliced
f-strings: the non-comment lines must round-trip through `yaml.safe_load` to
float-valued keys.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_bazel
import yaml

from augur.fit.derive_dilution_prior import _format_config_block
from augur.fit.dilution_prior import DilutionPrior, ImpliedSharePoint

_BASE_DATE = dt.date(2020, 1, 1)


def _make_prior(
    *,
    annual_dilution_rate: float = 0.18,
    annual_dilution_rate_log_sigma: float = 0.12,
    shares0: float = 1_000_000.0,
    residual_log_std: float = 0.02,
    valuation_monthly_log_return_mu: float | None = None,
    valuation_monthly_log_return_sigma: float | None = None,
) -> DilutionPrior:
    """A `DilutionPrior` with two implied-share points, for renderer tests."""
    points = (
        ImpliedSharePoint(
            date=_BASE_DATE,
            price_usd_per_share=1000.0,
            valuation_usd=1_000_000_000.0,
            implied_shares=1_000_000.0,
            delta_years=0.0,
        ),
        ImpliedSharePoint(
            date=dt.date(2021, 1, 1),
            price_usd_per_share=900.0,
            valuation_usd=1_000_000_000.0,
            implied_shares=1_111_111.0,
            delta_years=1.0,
        ),
    )
    return DilutionPrior(
        annual_dilution_rate=annual_dilution_rate,
        annual_dilution_rate_log_sigma=annual_dilution_rate_log_sigma,
        implied_share_points=points,
        shares0=shares0,
        residual_log_std=residual_log_std,
        valuation_monthly_log_return_mu=valuation_monthly_log_return_mu,
        valuation_monthly_log_return_sigma=valuation_monthly_log_return_sigma,
    )


def _parse_config_body(block: str) -> dict:
    """Parse the non-comment (config) lines of a rendered block as YAML.

    Provenance lines are plain-text comments (prefix ``#``); stripping them
    leaves only the serialized config mapping, which round-trips through
    ``yaml.safe_load``. Parsing rather than string-matching guards against the
    config being string-spliced instead of serialized.
    """
    config_text = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
    parsed = yaml.safe_load(config_text)
    assert isinstance(parsed, dict)
    return parsed


def test_config_lines_round_trip_as_floats() -> None:
    """The config keys come out of yaml.safe_dump as genuine YAML floats."""
    block = _format_config_block(_make_prior(), issuer_id="acme")
    config = _parse_config_body(block)

    assert config["annual_dilution_rate"] == pytest.approx(0.18)
    assert isinstance(config["annual_dilution_rate"], float)
    assert config["annual_dilution_rate_log_sigma"] == pytest.approx(0.12)
    assert isinstance(config["annual_dilution_rate_log_sigma"], float)


def test_large_and_small_magnitudes_stay_floats() -> None:
    """Awkward magnitudes serialize as floats, not ``852.0e9``-style strings.

    Routing through ``yaml.safe_dump`` (instead of f-string splicing) keeps a
    value like ``8.52e9`` a genuine float on the ``yaml.safe_load`` round-trip
    rather than a token that loads back as a string.
    """
    prior = _make_prior(annual_dilution_rate=8.52e9, annual_dilution_rate_log_sigma=2.0e-3)
    config = _parse_config_body(_format_config_block(prior, issuer_id="acme"))

    assert isinstance(config["annual_dilution_rate"], float)
    assert config["annual_dilution_rate"] == pytest.approx(8.52e9)
    assert isinstance(config["annual_dilution_rate_log_sigma"], float)
    assert config["annual_dilution_rate_log_sigma"] == pytest.approx(2.0e-3)


def test_valuation_params_omitted_when_absent() -> None:
    """Valuation drift/vol keys are absent when the prior leaves them None."""
    config = _parse_config_body(_format_config_block(_make_prior(), issuer_id="acme"))
    assert "valuation_monthly_log_return_mu" not in config
    assert "valuation_monthly_log_return_sigma" not in config


def test_valuation_params_serialized_as_floats_when_present() -> None:
    """Valuation drift/vol keys round-trip as floats when the prior sets them."""
    prior = _make_prior(valuation_monthly_log_return_mu=0.012, valuation_monthly_log_return_sigma=0.034)
    config = _parse_config_body(_format_config_block(prior, issuer_id="acme"))

    assert config["valuation_monthly_log_return_mu"] == pytest.approx(0.012)
    assert isinstance(config["valuation_monthly_log_return_mu"], float)
    assert config["valuation_monthly_log_return_sigma"] == pytest.approx(0.034)
    assert isinstance(config["valuation_monthly_log_return_sigma"], float)


def test_block_keeps_provenance_comment_header() -> None:
    """Provenance/NOTE stay plain-text comments around the serialized body."""
    block = _format_config_block(_make_prior(), issuer_id="acme")

    assert "# Derived dilution prior for issuer 'acme'" in block
    assert "# Provenance: implied-shares log-linear fit on n=2" in block
    assert "# NOTE (M2.2-A):" in block
    # Per-point implied-shares lines remain comments (documentation, not config).
    assert "#     2020-01-01" in block


if __name__ == "__main__":
    pytest_bazel.main()
