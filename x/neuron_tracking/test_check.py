"""Check counts, body matching and identity separation using synthetic masks."""

import json
import sys
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from x.neuron_tracking.annotations import Annotations, Frame, Neuron, Observation, Review, Source, Visibility
from x.neuron_tracking.check import DistinctCheck, Match, RegionCheck, SampleChecks, check_annotations, main


def make_annotations(count: int) -> Annotations:
    observations = []
    for index in range(count):
        x, y = 20 + index % 7 * 10, 20 + index // 7 * 10
        observations.append(
            Observation(
                neuron_id=f"cell-{index}",
                frame_index=3,
                visibility=Visibility.VISIBLE,
                body=[(y, x, x + 3)],
                branches=[],
                uncertain=[],
                review=Review.PROPOSED,
            )
        )
    return Annotations(
        schema_version=1,
        revision=1,
        source=Source(path="synthetic_clip.avi", sha256="0" * 64, width=128, height=96),
        frames=[Frame(frame_index=3, timestamp_s=1.5)],
        neurons=[Neuron(id=f"cell-{i}", color=f"#{i + 1:06x}") for i in range(count)],
        observations=observations,
    )


def make_checks(annotations: Annotations, match: Match = Match.BODY_CENTER) -> SampleChecks:
    return SampleChecks(
        source=annotations.source,
        frame=annotations.frames[0],
        regions=[
            RegionCheck(
                name="region_count", region_xyxy=(10, 10, 100, 80), match=Match.BODY_CENTER, min_count=12, max_count=20
            ),
            RegionCheck(name="example_body", region_xyxy=(20, 19, 23, 21), match=match, min_count=1, max_count=None),
        ],
        distinct=[],
    )


@pytest.mark.parametrize(("count", "passed"), [(0, False), (11, False), (12, True), (20, True), (21, False)])
def test_count_bounds_are_inclusive(count: int, passed: bool) -> None:
    annotations = make_annotations(count)
    results = check_annotations(annotations=annotations, checks=make_checks(annotations)).regions
    assert len(results[0].neuron_ids) == count
    assert results[0].passed == passed


@pytest.mark.parametrize("match", list(Match))
def test_body_example_ignores_branch_or_uncertainty_pixels(match: Match) -> None:
    annotations = make_annotations(1)
    checks = make_checks(annotations, match=match)
    assert check_annotations(annotations=annotations, checks=checks).regions[1].neuron_ids == ("cell-0",)
    data = annotations.model_dump(mode="json")
    observation = data["observations"][0]
    observation["body"] = [[1, 1, 4]]
    observation["branches"] = [[20, 20, 23]]
    observation["uncertain"] = [[19, 20, 23]]
    changed = Annotations.model_validate_json(json.dumps(data))
    assert not check_annotations(annotations=changed, checks=checks).regions[1].passed
    observation["body"] = []
    changed = Annotations.model_validate_json(json.dumps(data))
    assert not check_annotations(annotations=changed, checks=checks).regions[1].passed
    observation["branches"] = []
    observation["body"] = [[20, 18, 21]]  # Intersects the box, but its center x=19 is outside.
    changed = Annotations.model_validate_json(json.dumps(data))
    assert check_annotations(annotations=changed, checks=checks).regions[1].passed == (match == Match.BODY_OVERLAP)


@pytest.mark.parametrize(("body", "passed"), [([(19, 19, 22)], True), ([(20, 22, 25)], False), ([(21, 20, 23)], False)])
def test_region_uses_half_open_pixel_bounds(body: list[tuple[int, int, int]], passed: bool) -> None:
    annotations = make_annotations(1)
    data = annotations.model_dump(mode="json")
    data["observations"][0]["body"] = body
    changed = Annotations.model_validate_json(json.dumps(data))
    assert check_annotations(annotations=changed, checks=make_checks(annotations)).regions[1].passed == passed


@pytest.mark.parametrize(
    ("body", "passed"),
    [
        ([(19, 19, 21)], True),
        ([(20, 22, 25)], True),
        ([(20, 23, 26)], False),
        ([(20, 17, 20)], False),
        ([(18, 20, 23)], False),
        ([(21, 20, 23)], False),
        ([], False),
    ],
)
def test_body_overlap_requires_a_pixel_inside_half_open_box(body: list[tuple[int, int, int]], passed: bool) -> None:
    annotations = make_annotations(1)
    data = annotations.model_dump(mode="json")
    data["observations"][0]["body"] = body
    changed = Annotations.model_validate_json(json.dumps(data))
    checks = make_checks(annotations, match=Match.BODY_OVERLAP)
    assert check_annotations(annotations=changed, checks=checks).regions[1].passed == passed


@pytest.mark.parametrize("case", ["separate", "merged", "shared_extra", "missing"])
def test_distinct_examples_reject_reused_ids_and_missing_bodies(case: str) -> None:
    annotations = make_annotations(4)
    checks = SampleChecks(
        source=annotations.source,
        frame=annotations.frames[0],
        regions=[
            RegionCheck(
                name=f"example-{i}",
                region_xyxy=(20 + i * 10, 19, 23 + i * 10, 21),
                match=Match.BODY_OVERLAP,
                min_count=1,
                max_count=None,
            )
            for i in range(4)
        ],
        distinct=[DistinctCheck(name="four_distinct_neurons", regions=[f"example-{i}" for i in range(4)])],
    )
    data = annotations.model_dump(mode="json")
    match case:
        case "merged":
            data["observations"][0]["body"] = [[20, 20, 53]]
            for observation in data["observations"][1:]:
                observation["body"] = []
        case "shared_extra":
            data["observations"][0]["body"].append([20, 30, 33])
        case "missing":
            data["observations"][-1]["body"] = []
    report = check_annotations(annotations=Annotations.model_validate_json(json.dumps(data)), checks=checks)
    assert report.distinct[0].passed == (case == "separate")
    assert report.distinct[0].shared_neuron_ids == (("cell-0",) if case in ("merged", "shared_extra") else ())
    if case != "missing":
        assert all(region.passed for region in report.regions)


@pytest.mark.parametrize("mismatch", ["source", "dimensions", "frame", "timestamp"])
def test_wrong_input_is_rejected(mismatch: str) -> None:
    annotations = make_annotations(1)
    data = make_checks(annotations).model_dump(mode="json")
    match mismatch:
        case "source":
            data["source"]["sha256"] = "1" * 64
        case "dimensions":
            data["source"]["width"] = 129
        case "frame":
            data["frame"]["frame_index"] = 4
        case "timestamp":
            data["frame"]["timestamp_s"] = 1.6
    checks = SampleChecks.model_validate_json(json.dumps(data))
    with pytest.raises(ValueError, match=r"source|frame|timestamp"):
        check_annotations(annotations=annotations, checks=checks)


@pytest.mark.parametrize(
    "invalid", ["duplicate_name", "bounds", "range", "unknown_region", "repeated_region", "nonpositive_region"]
)
def test_invalid_check_configuration_is_rejected(invalid: str) -> None:
    data = make_checks(make_annotations(1)).model_dump(mode="json")
    match invalid:
        case "duplicate_name":
            data["regions"][1]["name"] = data["regions"][0]["name"]
        case "bounds":
            data["regions"][0]["region_xyxy"] = [10, 10, 129, 80]
        case "range":
            data["regions"][0]["max_count"] = 11
        case "unknown_region":
            data["distinct"] = [{"name": "separate", "regions": ["example_body", "unknown"]}]
        case "repeated_region":
            data["distinct"] = [{"name": "separate", "regions": ["example_body", "example_body"]}]
        case "nonpositive_region":
            data["regions"][0]["min_count"] = 0
            data["distinct"] = [{"name": "separate", "regions": ["region_count", "example_body"]}]
    with pytest.raises(ValidationError):
        SampleChecks.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(("count", "check_distinct", "exit_status"), [(15, False, 0), (1, False, 1), (15, True, 1)])
def test_cli_exit_status_and_machine_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    count: int,
    check_distinct: bool,
    exit_status: int,
) -> None:
    annotations = make_annotations(count)
    annotation_path, checks_path = tmp_path / "annotations.json", tmp_path / "checks.json"
    annotation_path.write_text(annotations.model_dump_json())
    checks = make_checks(annotations)
    if check_distinct:
        checks.distinct.append(DistinctCheck(name="separate", regions=["region_count", "example_body"]))
    checks_path.write_text(checks.model_dump_json())
    monkeypatch.setattr(sys, "argv", ["check", "--annotations", str(annotation_path), "--checks", str(checks_path)])
    if exit_status:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == exit_status
    else:
        main()
    report = json.loads(capsys.readouterr().out)
    assert report["regions"]["region_count"]["passed"] == (12 <= count <= 20)
    assert len(report["regions"]["region_count"]["neuron_ids"]) == count
    assert report["regions"]["example_body"]["passed"]
    if check_distinct:
        assert report["distinct"]["separate"] == {"passed": False, "shared_neuron_ids": ["cell-0"]}


if __name__ == "__main__":
    pytest_bazel.main()
