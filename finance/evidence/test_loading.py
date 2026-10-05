from __future__ import annotations

import io
import json
import re
import textwrap
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import pytest_bazel

from finance.evidence.loading import (
    fred_series_frame,
    french_factors_frame,
    french_international_market_frame,
    read_monthly_levels,
    source_bytes,
    yahoo_adjusted_close_frame,
)
from finance.evidence.sources import (
    EVIDENCE_SOURCES,
    FRED_CPI,
    FRENCH_FACTORS,
    FRENCH_INTERNATIONAL_INDICES,
    YAHOO_BTC,
    ZILLOW_ZHVI,
    EvidenceKind,
)

FRED_TEXT = (
    "observation_date,CPIAUCSL\n"
    "2024-12-30,5900.0\n"
    "2024-12-31,5881.63\n"  # later same-month observation wins after monthly_last
    "2025-10-01,\n"  # empty values (BLS gap style) are dropped
)


def test_fred_frame_parses_and_drops_empty_values() -> None:
    frame = fred_series_frame(FRED_TEXT.encode(), FRED_CPI)
    assert frame["date"].to_list() == [date(2024, 12, 30), date(2024, 12, 31)]
    assert frame["value"].to_list() == [5900.0, 5881.63]


def test_fred_frame_rejects_missing_series_column() -> None:
    with pytest.raises(ValueError, match=re.escape(FRED_CPI.provenance_label)):
        fred_series_frame(b"observation_date,OTHER\n2024-12-31,1.0\n", FRED_CPI)


def _yahoo_payload(points: list[tuple[datetime, float | None]], *, granularity: str = "1d") -> bytes:
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "meta": {"dataGranularity": granularity},
                        "timestamp": [int(moment.timestamp()) for moment, _ in points],
                        "indicators": {"adjclose": [{"adjclose": [value for _, value in points]}]},
                    }
                ]
            }
        }
    ).encode()


def test_yahoo_frame_skips_missing_closes_and_enforces_minimum_samples() -> None:
    points = [
        (datetime(2024, 12, 2, tzinfo=UTC), 95000.0),
        (datetime(2024, 12, 30, tzinfo=UTC), 93429.0),
        (datetime(2025, 1, 6, tzinfo=UTC), None),
        (datetime(2025, 1, 13, tzinfo=UTC), 94000.0),
    ]
    frame = yahoo_adjusted_close_frame(_yahoo_payload(points), YAHOO_BTC, minimum_samples=2)
    assert frame["value"].to_list() == [95000.0, 93429.0, 94000.0]
    with pytest.raises(ValueError, match=re.escape(YAHOO_BTC.provenance_label)):
        yahoo_adjusted_close_frame(_yahoo_payload(points), YAHOO_BTC, minimum_samples=10)


def test_yahoo_frame_rejects_a_silently_downgraded_granularity() -> None:
    """A coarser payload is a Yahoo downgrade, not a shorter history, and must fail loudly.

    Monthly bars parse cleanly, collapse to the same months, and clear `minimum_samples` — so
    without this check the only evidence of the downgrade is `meta.dataGranularity`, and the
    series silently anchors and fits on coarse data. This is exactly what `range=max` started
    doing: 404 monthly rows for SPY where the same window with explicit periods gives 8437.
    """

    points: list[tuple[datetime, float | None]] = [
        (datetime(2024, 12, 2, tzinfo=UTC), 95000.0),
        (datetime(2025, 1, 2, tzinfo=UTC), 94000.0),
    ]
    # Identical points, so the ONLY difference between the two arms is the granularity field.
    assert yahoo_adjusted_close_frame(_yahoo_payload(points), YAHOO_BTC, minimum_samples=2)["value"].to_list() == [
        95000.0,
        94000.0,
    ]
    with pytest.raises(ValueError, match=re.escape(YAHOO_BTC.provenance_label)):
        yahoo_adjusted_close_frame(_yahoo_payload(points, granularity="1mo"), YAHOO_BTC, minimum_samples=2)


def test_yahoo_sources_request_an_explicit_window_not_range_max() -> None:
    """`range=max` is what Yahoo downgrades; explicit periods mean the same thing and are honoured.

    Pinned because the two spellings look interchangeable and the difference is invisible until
    a fit fails months later.
    """

    for source in EVIDENCE_SOURCES:
        if source.kind is EvidenceKind.YAHOO:
            assert "range=max" not in source.upstream_url
            assert "period1=0" in source.upstream_url
            assert "interval=1d" in source.upstream_url


def test_read_monthly_levels_from_checkout_dir(tmp_path: Path) -> None:
    (tmp_path / FRED_CPI.output_filename).write_text(FRED_TEXT)
    levels = read_monthly_levels(tmp_path, FRED_CPI)
    assert [(level.month, level.value) for level in levels] == [(date(2024, 12, 1), 5881.63)]


def test_read_monthly_levels_rejects_zillow() -> None:
    with pytest.raises(ValueError, match=re.escape(ZILLOW_ZHVI.provenance_label)):
        read_monthly_levels(Path("/nonexistent"), ZILLOW_ZHVI)


def test_source_bytes_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        source_bytes(tmp_path, FRED_CPI)


def _french_zip(body: str, *, member: str = "F-F_Research_Data_Factors.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, body)
    return buffer.getvalue()


_FRENCH_BODY = textwrap.dedent("""\
    This file was created using the 202606 CRSP database.
    The 1-month TBill rate data until 202405 are from Ibbotson Associates.

    ,Mkt-RF,SMB,HML,RF
    192607,   2.89,  -2.55,  -2.39,   0.22
    192608,   2.64,  -1.20,   3.82,   0.25
    192609,   0.36,  -1.29,   0.13,   0.23

     Annual Factors: January-December
    ,Mkt-RF,SMB,HML,RF
      1927,  29.44,  -2.20,  -4.58,   3.12
      1928,  35.56,   3.73,  -5.26,   3.56

    Copyright 2026 Eugene F. Fama and Kenneth R. French
    """)


def test_the_annual_section_is_not_mistaken_for_monthly_rows() -> None:
    """The one failure this loader exists to prevent. The annual block repeats the same four
    column names and its values (29.44 for 1927) parse as plausible monthly returns, so
    including it would add outliers to the sample and inflate every fitted volatility without
    producing a single malformed value."""

    frame = french_factors_frame(_french_zip(_FRENCH_BODY), FRENCH_FACTORS)

    assert frame.height == 3
    assert frame.get_column("market_total_return").max() < 0.05
    assert date(1927, 1, 1) not in frame.get_column("month").to_list()


def test_a_gap_in_the_monthly_series_is_rejected() -> None:
    """Row count against the date span. A dropped month would silently shorten every window in
    the historical replay and misalign it against the other evidence series."""

    body = _FRENCH_BODY.replace("192608,   2.64,  -1.20,   3.82,   0.25\n", "")
    with pytest.raises(ValueError, match=re.escape(FRENCH_FACTORS.provenance_label)):
        french_factors_frame(_french_zip(body), FRENCH_FACTORS)


def test_an_archive_without_exactly_one_member_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.csv", _FRENCH_BODY)
        archive.writestr("b.csv", _FRENCH_BODY)
    with pytest.raises(ValueError, match=re.escape(FRENCH_FACTORS.provenance_label)):
        french_factors_frame(buffer.getvalue(), FRENCH_FACTORS)


def test_a_file_with_no_monthly_rows_is_rejected() -> None:
    body = "header only\n\n Annual Factors: January-December \n,Mkt-RF,SMB,HML,RF\n  1927,  29.44,  -2.20,  -4.58,   3.12\n"
    with pytest.raises(ValueError, match=re.escape(FRENCH_FACTORS.provenance_label)):
        french_factors_frame(_french_zip(body), FRENCH_FACTORS)


def test_read_monthly_levels_rejects_a_french_factors_file() -> None:
    """`read_monthly_levels` dispatches on kind, and a kind with no case falls off the end of
    the match into `monthly_last(raw)` with `raw` unbound — an `UnboundLocalError` a long way
    from the cause. Adding FRENCH to the evidence set did exactly that and the repo-wide gate
    did not catch it, so every kind now either parses or says why it cannot, and `assert_never`
    makes the NEXT kind a type error instead of a runtime surprise."""

    with pytest.raises(ValueError, match=re.escape(FRENCH_FACTORS.provenance_label)):
        read_monthly_levels(Path("/nonexistent"), FRENCH_FACTORS)


def _international_section(title: str, rows: list[tuple[str, float]]) -> str:
    header = "                -- BE/ME --   --- E/P ---\r\n          Mkt   High    Low   High    Low\r\n"
    body = "".join(f"{stamp}  {mkt:5.2f}  99.00  99.00  99.00  99.00\r\n" for stamp, mkt in rows)
    return f"     {title}\r\n{header}{body}\r\n"


_INTERNATIONAL_MONTHS = ("197501", "197502", "197503")


def _international_zip(target_rows: list[tuple[str, float]], *, member: str = "Ind_all.Dat") -> bytes:
    """`Ind_all.Dat`'s layout in miniature: the wanted block among decoys that share its columns.

    Every decoy carries returns far from the wanted block's, so reading any of them shows.
    """

    body = "\r\n" + "".join(
        [
            _international_section("Value-Weight Dollar Returns      All 4 Data Items Not Reqd", target_rows),
            _international_section(
                "Value-Weight Local  Returns      All 4 Data Items Not Reqd", [(m, 50.0) for m in _INTERNATIONAL_MONTHS]
            ),
            _international_section(
                "Value-Weight Dollar Returns      All 4 Data Items Required", [(m, 60.0) for m in _INTERNATIONAL_MONTHS]
            ),
            _international_section("Value-Weight Dollar Returns      All 4 Data Items Not Reqd", [("  1975", 70.0)]),
        ]
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, body + "Copyright 2026 Kenneth R. French\r\n")
        archive.writestr("Ind_UK.Dat", body.replace(" 1.00", "40.00"))
    return buffer.getvalue()


_INTERNATIONAL_TARGET = [("197501", 1.0), ("197502", -2.0), ("197503", 3.0)]


def test_international_market_is_the_all_country_dollar_mkt_column_as_decimals() -> None:
    frame = french_international_market_frame(_international_zip(_INTERNATIONAL_TARGET), FRENCH_INTERNATIONAL_INDICES)

    assert frame.get_column("month").to_list() == [date(1975, 1, 1), date(1975, 2, 1), date(1975, 3, 1)]
    assert frame.get_column("market_total_return").to_list() == pytest.approx([0.01, -0.02, 0.03])


def test_international_sections_sharing_the_columns_are_not_read() -> None:
    """Local-currency, all-ratios-required and annual blocks all parse as plausible returns under
    the same `Mkt` header; reading one would mislabel currency or universe without a malformed
    value anywhere."""

    frame = french_international_market_frame(_international_zip(_INTERNATIONAL_TARGET), FRENCH_INTERNATIONAL_INDICES)

    assert frame.height == len(_INTERNATIONAL_TARGET)
    assert frame.get_column("market_total_return").abs().max() < 0.05


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([("197501", 1.0), ("197503", 3.0)], "gapless"),
        ([("197501", 1.0), ("197502", -99.99), ("197503", 3.0)], "missing in 1975-02-01"),
    ],
)
def test_international_gaps_and_missing_markers_are_rejected(rows: list[tuple[str, float]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        french_international_market_frame(_international_zip(rows), FRENCH_INTERNATIONAL_INDICES)


def test_international_archive_without_the_all_country_member_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"no Ind_all\.Dat"):
        french_international_market_frame(
            _international_zip(_INTERNATIONAL_TARGET, member="Ind_Asia_Pacific.Dat"), FRENCH_INTERNATIONAL_INDICES
        )


def test_international_file_without_its_monthly_dollar_section_is_rejected() -> None:
    body = _international_zip(_INTERNATIONAL_TARGET)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        text = archive.read("Ind_all.Dat").decode().replace("Dollar Returns      All 4 Data Items Not Reqd", "Dollar")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Ind_all.Dat", text)
    with pytest.raises(ValueError, match="0 monthly"):
        french_international_market_frame(buffer.getvalue(), FRENCH_INTERNATIONAL_INDICES)


if __name__ == "__main__":
    pytest_bazel.main()
