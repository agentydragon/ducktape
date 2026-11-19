"""Tests for the material test grid generator."""

from __future__ import annotations

import tomllib
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from laser.lightburn.lbrn2_writer import LightBurnProject, RectShape, TextShape
from laser.material_test.grid_generator import (
    AxisConfig,
    BorderConfig,
    CellContent,
    CutConfig,
    CutParam,
    GeometryConfig,
    GridConfig,
    LabelsConfig,
    _constant_params,
    _param_short_label,
    fmt_val,
    generate,
    generate_rd,
    main,
)
from laser.ruida.rd_format import RdJob, unscramble_bytes


@pytest.mark.parametrize(("value", "expected"), [(15.0, "15"), (0.0, "0"), (-0.5, "-0.5")])
def test_fmt_val(value: float, expected: str):
    assert fmt_val(value) == expected


def _minimal_config(**kwargs) -> GridConfig:
    return GridConfig.model_validate(
        {
            "x": AxisConfig(param=CutParam.POWER_PCT, values=[10, 20, 30]),
            "y": AxisConfig(param=CutParam.SPEED_MM_S, values=[50, 100]),
            **kwargs,
        }
    )


def _rd_move_origins(job: RdJob) -> list[tuple[float, float]]:
    raw = unscramble_bytes(job.to_bytes())
    body_start = len(job._header())
    body_end = len(raw) - len(job._trailer())
    body = raw[body_start:body_end]

    def decode_mm(encoded: bytes) -> float:
        value = 0
        for byte in encoded:
            value = (value << 7) | byte
        return value / 1000

    return [
        (decode_mm(body[i + 1 : i + 6]), decode_mm(body[i + 6 : i + 11]))
        for i, opcode in enumerate(body[:-10])
        if opcode == 0x88
    ]


@pytest.mark.parametrize(
    ("axis_params", "message"),
    [
        pytest.param({"x": CutParam.SPEED_MM_S, "y": CutParam.SPEED_MM_S}, "must be different", id="same-axes"),
        pytest.param({"x": CutParam.POWER_MIN_PCT}, "use 'power_pct'", id="power-min-axis"),
        pytest.param({"x": CutParam.POWER_MAX_PCT}, "use 'power_pct'", id="power-max-axis"),
        pytest.param({"cols": CutParam.POWER_PCT}, "must be different", id="cols-repeats-x"),
        pytest.param({"rows": CutParam.SPEED_MM_S}, "must be different", id="rows-repeats-y"),
        pytest.param(
            {"cols": CutParam.NUM_PASSES, "rows": CutParam.NUM_PASSES}, "must be different", id="cols-repeats-rows"
        ),
    ],
)
def test_grid_config_rejects_invalid_axes(axis_params: dict[str, CutParam], message: str):
    axes = {name: AxisConfig(param=param, values=[10, 20]) for name, param in axis_params.items()}
    with pytest.raises(Exception, match=message):
        _minimal_config(**axes)


def test_cut_config_power_shorthand():
    cc = CutConfig(power_pct=75)
    assert cc.power_min_pct == 75
    assert cc.power_max_pct == 75


def test_cut_config_power_shorthand_overrides_explicit_max():
    # model_validator runs after all fields are set
    cc = CutConfig(power_pct=60, power_max_pct=80)
    assert cc.power_min_pct == 60
    assert cc.power_max_pct == 60


def _make_project(x_values: list[float] | None = None, y_values: list[float] | None = None) -> LightBurnProject:
    resolved_x = x_values if x_values is not None else [10.0, 20.0, 30.0]
    resolved_y = y_values if y_values is not None else [50.0, 100.0]
    cfg = GridConfig(
        title="Test grid",
        x=AxisConfig(param=CutParam.POWER_PCT, values=resolved_x, label="Power [%]"),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=resolved_y, label="Speed [mm/s]"),
    )
    return generate(cfg)


def test_rd_grid_accepts_128_layers():
    config = _minimal_config(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[20] * 128), y=AxisConfig(param=CutParam.SPEED_MM_S, values=[100])
    )
    assert len(generate_rd(config).layers) == 128


def test_generate_cell_cut_settings_have_correct_params():
    """Each cell layer should have x_param and y_param set correctly.

    Cells are sorted by ascending energy (max_power * num_passes / speed).
    x=power_pct [10, 20], y=speed_mm_s [50, 100]:
      (r0,c0) 10/50=0.2, (r0,c1) 20/50=0.4, (r1,c0) 10/100=0.1, (r1,c1) 20/100=0.2
    Sorted: 0.1 → 0.2 → 0.2 → 0.4
    """
    x_vals = [10.0, 20.0]
    y_vals = [50.0, 100.0]
    project = _make_project(x_values=x_vals, y_values=y_vals)
    cell_layers = project.cut_settings[1:]  # skip text layer
    assert len(cell_layers) == 4
    # Lowest energy: power=10, speed=100 (energy=0.1)
    assert cell_layers[0].max_power == 10.0
    assert cell_layers[0].speed == 100.0
    # power=10, speed=50 (energy=0.2)
    assert cell_layers[1].max_power == 10.0
    assert cell_layers[1].speed == 50.0
    # power=20, speed=100 (energy=0.2)
    assert cell_layers[2].max_power == 20.0
    assert cell_layers[2].speed == 100.0
    # Highest energy: power=20, speed=50 (energy=0.4)
    assert cell_layers[3].max_power == 20.0
    assert cell_layers[3].speed == 50.0


def test_cells_sorted_by_energy_across_subgrids():
    """Energy sorting is global across sub-grids, not per-sub-grid."""
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 50.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[100.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 5]),
    )
    project = generate(cfg)
    cell_layers = project.cut_settings[1:]
    energies = [cs.max_power * cs.num_passes / cs.speed for cs in cell_layers]
    assert energies == sorted(energies)
    # Verify interleaving: 1-pass cells (energy 0.1, 0.5) should come before
    # 5-pass 50% power (energy 2.5), not grouped by sub-grid.
    assert cell_layers[0].num_passes == 1
    assert cell_layers[0].max_power == 10.0
    assert cell_layers[1].num_passes == 1
    assert cell_layers[1].max_power == 50.0
    assert cell_layers[2].num_passes == 5
    assert cell_layers[2].max_power == 10.0
    assert cell_layers[3].num_passes == 5
    assert cell_layers[3].max_power == 50.0


def test_generate_xml_round_trips():
    project = _make_project()
    xml_str = project.to_xml_str()
    root = ET.fromstring(xml_str.split("\n", 1)[1])
    assert root.tag == "LightBurnProject"


def _text_count(cell_text: CellContent) -> int:
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        labels=LabelsConfig(cell_text=cell_text),
    )
    return sum(isinstance(s, TextShape) for s in generate(cfg).shapes)


@pytest.mark.parametrize("cell_text", [CellContent.VALUES, CellContent.VALUES_WITH_UNITS])
def test_generate_with_cell_text(cell_text: CellContent):
    """Each of the 2 cells carries two in-cell lines (its x and y value) on top of the axis labels."""
    assert _text_count(cell_text) - _text_count(CellContent.NOTHING) == 2 * 2


def test_generate_no_labels():
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0], show_labels=False),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0], show_labels=False),
        auto_subtitle=False,  # suppress auto-generated subtitle too
    )
    project = generate(cfg)
    texts = [s for s in project.shapes if isinstance(s, TextShape)]
    assert texts == []


def test_generate_auto_subtitle_omits_varied_params():
    """Auto-subtitle should not mention x or y axis parameters."""
    cfg = GridConfig(
        title="T",
        auto_subtitle=True,
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0, 100.0]),
        cut=CutConfig(z_offset_mm=-0.1, kerf_mm=0.05),
    )
    constant = _constant_params(cfg)
    assert {CutParam.POWER_PCT, CutParam.SPEED_MM_S}.isdisjoint(constant)
    assert {CutParam.Z_OFFSET_MM, CutParam.KERF_MM} <= set(constant)


def test_example_config_parses(runfiles: Runfiles):
    """example_config.toml must parse without error and produce a valid GridConfig."""
    path = runfiles.root() / "ducktape/laser/material_test/example_config.toml"
    with path.open("rb") as f:
        data = tomllib.load(f)
    GridConfig.model_validate(data)


def test_example_config_3d_parses(runfiles: Runfiles):
    """example_config_3d.toml must parse and remain a three-dimensional example."""
    path = runfiles.root() / "ducktape/laser/material_test/example_config_3d.toml"
    with path.open("rb") as f:
        data = tomllib.load(f)
    cfg = GridConfig.model_validate(data)
    assert cfg.cols is not None


@pytest.mark.parametrize(
    ("cols", "rows", "n_subgrids"),
    [
        pytest.param(None, None, 1, id="inner_only"),
        pytest.param(AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2, 3]), None, 3, id="cols_only"),
        pytest.param(None, AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3, -0.5]), 2, id="rows_only"),
        pytest.param(
            AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
            AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3, -0.5]),
            4,
            id="cols_and_rows",
        ),
    ],
)
def test_generate_cell_counts(cols: AxisConfig | None, rows: AxisConfig | None, n_subgrids: int):
    """Every sub-grid repeats all inner cells (x=3 by y=2): one cut layer and one rect per cell."""
    project = generate(_minimal_config(cols=cols, rows=rows))
    n_cells = 3 * 2 * n_subgrids
    assert len(project.cut_settings) == 1 + n_cells  # + text layer
    assert len([s for s in project.shapes if isinstance(s, RectShape)]) == n_cells


def test_generate_4d_outer_params_applied():
    """Both cols and rows params should be applied.

    Global energy sort: power=10, speed=50 for all cells, so energy depends
    only on num_passes. passes=1 cells (energy=0.2) sort before passes=2
    cells (energy=0.4). Within same energy, stable sort preserves iteration
    order (row0 before row1).
    """
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        rows=AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3, -0.5]),
    )
    project = generate(cfg)
    cell_layers = [cs for cs in project.cut_settings if cs.index > 0]
    # 4 sub-grids x 1 cell each = 4 cells, sorted by energy globally
    # passes=1 cells first (energy=0.2), then passes=2 (energy=0.4)
    assert cell_layers[0].num_passes == 1
    assert cell_layers[0].z_per_pass == -0.3
    assert cell_layers[1].num_passes == 1
    assert cell_layers[1].z_per_pass == -0.5
    assert cell_layers[2].num_passes == 2
    assert cell_layers[2].z_per_pass == -0.3
    assert cell_layers[3].num_passes == 2
    assert cell_layers[3].z_per_pass == -0.5


@pytest.mark.parametrize(
    ("cols", "n_cells"),
    [
        pytest.param(None, 3 * 2, id="2d"),  # x * y
        pytest.param(AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]), 3 * 2 * 2, id="3d"),  # x * y * cols
    ],
)
def test_generate_border_adds_layer_and_rect(cols: AxisConfig | None, n_cells: int):
    project = generate(_minimal_config(cols=cols, border=BorderConfig(enabled=True)))
    assert any(cs.name == "Border" for cs in project.cut_settings)
    rects = [s for s in project.shapes if isinstance(s, RectShape)]
    assert len(rects) == n_cells + 1  # +1 for border


@pytest.mark.parametrize("axis_name", ["x", "y", "cols", "rows"])
@pytest.mark.parametrize(
    ("param", "default_value"),
    [(CutParam.KERF_MM, 0.0), (CutParam.Z_OFFSET_MM, 0.0), (CutParam.Z_PER_PASS_MM, 0.0), (CutParam.NUM_PASSES, 1.0)],
)
def test_generate_rd_rejects_unsupported_sweep_axes_even_at_default(
    axis_name: str, param: CutParam, default_value: float
):
    config = _minimal_config(**{axis_name: AxisConfig(param=param, values=[default_value])})

    with pytest.raises(ValueError, match=f"{axis_name}.*{param.value}"):
        generate_rd(config)


def test_generate_rd_rejects_nonzero_constant_kerf():
    config = _minimal_config(cut=CutConfig(kerf_mm=0.1))
    with pytest.raises(ValueError, match="kerf_mm"):
        generate_rd(config)


@pytest.mark.parametrize(
    ("config", "unsupported_field"),
    [
        (_minimal_config(cut=CutConfig(num_passes=2)), "num_passes"),
        (_minimal_config(cut=CutConfig(z_offset_mm=0.1)), "z_offset_mm"),
        (_minimal_config(cut=CutConfig(z_per_pass_mm=-0.1)), "z_per_pass_mm"),
        (_minimal_config(border=BorderConfig(enabled=True, num_passes=2)), "num_passes"),
    ],
    ids=["cut-passes", "cut-z-offset", "cut-z-per-pass", "border-passes"],
)
def test_generate_rd_rejects_unsupported_constant_and_border_settings(config: GridConfig, unsupported_field: str):
    with pytest.raises(ValueError, match=unsupported_field):
        generate_rd(config).to_bytes()


def test_generate_rd_default_settings_and_border_still_serialize():
    config = _minimal_config(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        border=BorderConfig(enabled=True, padding_mm=0),
    )
    job = generate_rd(config)

    assert len(job.layers) == 2
    assert len(job.rects) == 2
    assert job.layers[-1].num_passes == 1
    assert job.to_bytes().endswith(bytes([0x60]))  # scrambled D7 EOF


@pytest.mark.parametrize(
    ("x_values", "extra_toml", "message"),
    [
        pytest.param(
            [20] * 128, "[border]\nenabled = true\n", "at most 128 layers, got 129", id="border-over-layer-limit"
        ),
        pytest.param([10.0], "[cut]\nnum_passes = 2\n", "num_passes", id="unsupported-setting"),
        pytest.param([10.0], "[geometry]\ncell_size_mm = 40000000.0\n", "fit in 5 7-bit bytes", id="oversized-cell"),
    ],
)
def test_rd_cli_failure_preserves_existing_output(tmp_path: Path, x_values: list[float], extra_toml: str, message: str):
    config_path = tmp_path / "grid.toml"
    config_path.write_text(
        f'[x]\nparam = "power_pct"\nvalues = {x_values}\n[y]\nparam = "speed_mm_s"\nvalues = [50.0]\n{extra_toml}',
        encoding="utf-8",
    )
    output_path = tmp_path / "existing.rd"
    original_output = b"existing output"
    output_path.write_bytes(original_output)

    with pytest.raises(ValueError, match=message):
        main([str(config_path), "--format", "rd", "--output", str(output_path)])

    assert output_path.read_bytes() == original_output


def test_generate_rd_border_offsets_cells_inside_nonnegative_border():
    padding = 3.0
    config = _minimal_config(border=BorderConfig(enabled=True, padding_mm=padding))
    plain_cells = generate_rd(_minimal_config()).rects

    job = generate_rd(config)
    cells = job.rects[:-1]
    border = job.rects[-1]

    assert [(r.x_mm, r.y_mm, r.width_mm, r.height_mm) for r in cells] == [
        (r.x_mm + padding, r.y_mm + padding, r.width_mm, r.height_mm) for r in plain_cells
    ]
    assert border.x_mm == pytest.approx(0)
    assert border.y_mm == pytest.approx(0)
    assert min(r.x_mm for r in cells) == pytest.approx(padding)
    assert min(r.y_mm for r in cells) == pytest.approx(padding)
    origins = _rd_move_origins(job)
    assert len(origins) == len(job.rects)
    for (actual_x, actual_y), rect in zip(origins, job.rects, strict=True):
        assert actual_x == pytest.approx(rect.x_mm, abs=0.001)
        assert actual_y == pytest.approx(rect.y_mm, abs=0.001)


def test_generate_rd_zero_padding_and_no_border_keep_cell_origins():
    plain_job = generate_rd(_minimal_config())
    zero_padding_job = generate_rd(_minimal_config(border=BorderConfig(enabled=True, padding_mm=0)))

    plain_cells = plain_job.rects
    zero_padding_cells = zero_padding_job.rects[:-1]
    assert [(r.x_mm, r.y_mm) for r in zero_padding_cells] == [(r.x_mm, r.y_mm) for r in plain_cells]
    assert (zero_padding_job.rects[-1].x_mm, zero_padding_job.rects[-1].y_mm) == (0, 0)

    for job in (plain_job, zero_padding_job):
        origins = _rd_move_origins(job)
        assert len(origins) == len(job.rects)
        for (actual_x, actual_y), rect in zip(origins, job.rects, strict=True):
            assert actual_x == pytest.approx(rect.x_mm, abs=0.001)
            assert actual_y == pytest.approx(rect.y_mm, abs=0.001)


def test_generate_rd_rejects_negative_border_padding():
    config = _minimal_config(border=BorderConfig(enabled=True, padding_mm=-1.0))
    with pytest.raises(ValueError, match="padding_mm must be non-negative"):
        generate_rd(config)


def test_auto_subtitle_excludes_outer_axes():
    """Auto-subtitle should not mention cols or rows parameters."""
    cfg = GridConfig(
        title="T",
        auto_subtitle=True,
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        rows=AxisConfig(param=CutParam.Z_PER_PASS_MM, values=[-0.3]),
    )
    constant = _constant_params(cfg)
    assert {CutParam.NUM_PASSES, CutParam.Z_PER_PASS_MM}.isdisjoint(constant)
    assert {CutParam.Z_OFFSET_MM, CutParam.KERF_MM} <= set(constant)


def test_subgrid_gap_affects_layout():
    """Different subgrid_gap_mm should produce different rect positions."""
    cfg_small = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        geometry=GeometryConfig(subgrid_gap_mm=5.0),
    )
    cfg_large = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        cols=AxisConfig(param=CutParam.NUM_PASSES, values=[1, 2]),
        geometry=GeometryConfig(subgrid_gap_mm=50.0),
    )
    p_small = generate(cfg_small)
    p_large = generate(cfg_large)

    def rect_xs(project: LightBurnProject) -> list[float]:
        return sorted(s.xform.tx for s in project.shapes if isinstance(s, RectShape))

    xs_small = rect_xs(p_small)
    xs_large = rect_xs(p_large)
    # Larger gap → second sub-grid's rect is further to the right
    assert xs_large[1] > xs_small[1]


def test_param_short_label():
    assert _param_short_label(CutParam.SPEED_MM_S) == "mm/s"  # abbreviable + unit
    assert _param_short_label(CutParam.POWER_PCT) == "PWR%"  # not abbreviable + unit
    assert _param_short_label(CutParam.NUM_PASSES) == "Passes"  # no unit


def test_generate_legend_cell():
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0]),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0]),
        labels=LabelsConfig(cell_text=CellContent.VALUES_WITH_UNITS, show_legend=True),
    )
    project = generate(cfg)
    # Legend rect on layer 0
    legend_rects = [s for s in project.shapes if isinstance(s, RectShape) and s.cut_index == 0]
    assert len(legend_rects) == 1
    # Two legend text labels
    texts = [s for s in project.shapes if isinstance(s, TextShape)]
    legend_texts = [t for t in texts if t.text in ("PWR%", "mm/s")]
    assert len(legend_texts) == 2


@pytest.mark.parametrize(
    ("labels", "show_labels", "n_legend_rects"),
    [
        pytest.param(LabelsConfig(cell_text=CellContent.NOTHING, show_legend=True), True, 0, id="no-cell-content"),
        pytest.param(
            LabelsConfig(cell_text=CellContent.VALUES_WITH_UNITS, show_legend=True), False, 1, id="labels-hidden"
        ),
        pytest.param(LabelsConfig(cell_text=CellContent.VALUES_WITH_UNITS), True, 0, id="default-off"),
    ],
)
def test_generate_legend_rect(labels: LabelsConfig, show_labels: bool, n_legend_rects: int):
    """The legend rect (on layer 0) needs `show_legend` and in-cell text; hidden labels don't remove it."""
    cfg = GridConfig(
        x=AxisConfig(param=CutParam.POWER_PCT, values=[10.0, 20.0], show_labels=show_labels),
        y=AxisConfig(param=CutParam.SPEED_MM_S, values=[50.0], show_labels=show_labels),
        labels=labels,
        auto_subtitle=show_labels,
    )
    legend_rects = [s for s in generate(cfg).shapes if isinstance(s, RectShape) and s.cut_index == 0]
    assert len(legend_rects) == n_legend_rects


if __name__ == "__main__":
    pytest_bazel.main()
