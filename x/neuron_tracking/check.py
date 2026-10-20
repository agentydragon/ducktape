"""Check neuron body locations, regional counts and distinct identities."""

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, StrictInt, model_validator

from x.neuron_tracking.annotations import Annotations, Frame, Mask, Record, Source, mask_center


class Match(StrEnum):
    BODY_CENTER = "body_center"
    BODY_OVERLAP = "body_overlap"


class RegionCheck(Record):
    name: Annotated[str, Field(min_length=1)]
    region_xyxy: tuple[StrictInt, StrictInt, StrictInt, StrictInt]
    match: Match
    min_count: Annotated[StrictInt, Field(ge=0)]
    max_count: Annotated[StrictInt, Field(ge=0)] | None


class DistinctCheck(Record):
    name: Annotated[str, Field(min_length=1)]
    regions: Annotated[list[str], Field(min_length=2)]


class SampleChecks(Record):
    source: Source
    frame: Frame
    regions: Annotated[list[RegionCheck], Field(min_length=1)]
    distinct: list[DistinctCheck]

    @model_validator(mode="after")
    def validate_regions(self) -> Self:
        names = [check.name for check in self.regions] + [check.name for check in self.distinct]
        if len(set(names)) != len(names):
            raise ValueError("Check names must be unique")
        for region in self.regions:
            left, top, right, bottom = region.region_xyxy
            if not (0 <= left < right <= self.source.width and 0 <= top < bottom <= self.source.height):
                raise ValueError(f"Region outside source dimensions: {region.name}")
            if region.max_count is not None and region.max_count < region.min_count:
                raise ValueError(f"Inverted count range: {region.name}")
        positive_regions = {region.name for region in self.regions if region.min_count >= 1}
        for check in self.distinct:
            if len(set(check.regions)) != len(check.regions) or not set(check.regions) <= positive_regions:
                raise ValueError(f"Distinct checks need unique references to positive region checks: {check.name}")
        return self


@dataclass(frozen=True)
class Result:
    check: RegionCheck
    neuron_ids: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return len(self.neuron_ids) >= self.check.min_count and (
            self.check.max_count is None or len(self.neuron_ids) <= self.check.max_count
        )


@dataclass(frozen=True)
class DistinctResult:
    check: DistinctCheck
    regions: tuple[Result, ...]

    @property
    def shared_neuron_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                identity
                for identity, count in Counter(i for r in self.regions for i in r.neuron_ids).items()
                if count > 1
            )
        )

    @property
    def passed(self) -> bool:
        return all(region.passed for region in self.regions) and not self.shared_neuron_ids


@dataclass(frozen=True)
class Report:
    regions: tuple[Result, ...]
    distinct: tuple[DistinctResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.regions) and all(result.passed for result in self.distinct)


def matches_body(body: Mask, check: RegionCheck) -> bool:
    left, top, right, bottom = check.region_xyxy
    match check.match:
        case Match.BODY_CENTER:
            return (center := mask_center(body)) is not None and left <= center[0] < right and top <= center[1] < bottom
        case Match.BODY_OVERLAP:
            return any(top <= y < bottom and start < right and end > left for y, start, end in body)


def check_annotations(annotations: Annotations, checks: SampleChecks) -> Report:
    if (
        annotations.source.sha256 != checks.source.sha256
        or annotations.source.width != checks.source.width
        or annotations.source.height != checks.source.height
    ):
        raise ValueError(f"Annotation source does not match checks for {checks.source.path}")
    frames = {frame.frame_index: frame for frame in annotations.frames}
    if checks.frame.frame_index not in frames:
        raise ValueError(f"Missing checked frame {checks.frame.frame_index}")
    if abs(frames[checks.frame.frame_index].timestamp_s - checks.frame.timestamp_s) > 0.00001:
        raise ValueError("Checked frame timestamp differs from annotations")
    observations = [o for o in annotations.observations if o.frame_index == checks.frame.frame_index]
    results = [
        Result(
            check=check,
            neuron_ids=tuple(sorted(o.neuron_id for o in observations if matches_body(body=o.body, check=check))),
        )
        for check in checks.regions
    ]
    by_name = {result.check.name: result for result in results}
    return Report(
        regions=tuple(results),
        distinct=tuple(
            DistinctResult(check=check, regions=tuple(by_name[name] for name in check.regions))
            for check in checks.distinct
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", required=True, type=Path)
    parser.add_argument("--checks", required=True, type=Path)
    arguments = parser.parse_args()
    report = check_annotations(
        annotations=Annotations.model_validate_json(arguments.annotations.read_bytes()),
        checks=SampleChecks.model_validate_json(arguments.checks.read_bytes()),
    )
    print(
        json.dumps(
            {
                "regions": {
                    result.check.name: {"passed": result.passed, "neuron_ids": result.neuron_ids}
                    for result in report.regions
                },
                "distinct": {
                    result.check.name: {"passed": result.passed, "shared_neuron_ids": result.shared_neuron_ids}
                    for result in report.distinct
                },
            },
            indent=2,
        )
    )
    if not report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
