"""Pixel annotation models and image-dependent validation."""

from collections.abc import Iterator
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

type Run = tuple[StrictInt, StrictInt, StrictInt]
type Mask = list[Run]


def mask_center(mask: Mask) -> tuple[float, float] | None:
    area = sum(end - start for _, start, end in mask)
    if area == 0:
        return None
    return (
        sum((start + end - 1) * (end - start) / 2 for _, start, end in mask) / area,
        sum(y * (end - start) for y, start, end in mask) / area,
    )


class Part(StrEnum):
    BODY = "body"
    BRANCHES = "branches"
    UNCERTAIN = "uncertain"


class Visibility(StrEnum):
    VISIBLE = "visible"
    UNCERTAIN = "uncertain"
    NOT_VISIBLE = "not_visible"
    OUT_OF_FRAME = "out_of_frame"


class Review(StrEnum):
    PROPOSED = "proposed"
    AGENT_REVIEWED = "agent_reviewed"
    HUMAN_REVIEWED = "human_reviewed"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class Source(Record):
    path: Annotated[str, Field(min_length=1)]
    sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    width: Annotated[StrictInt, Field(gt=0)]
    height: Annotated[StrictInt, Field(gt=0)]


class Frame(Record):
    frame_index: Annotated[StrictInt, Field(ge=0)]
    timestamp_s: float


class Neuron(Record):
    id: Annotated[str, Field(min_length=1)]
    color: Annotated[str, Field(pattern="^#[0-9a-fA-F]{6}$")]


class Observation(Record):
    neuron_id: str
    frame_index: Annotated[StrictInt, Field(ge=0)]
    visibility: Visibility
    body: Mask
    branches: Mask
    uncertain: Mask
    review: Review

    def iter_masks(self) -> Iterator[tuple[Part, Mask]]:
        yield Part.BODY, self.body
        yield Part.BRANCHES, self.branches
        yield Part.UNCERTAIN, self.uncertain


class Annotations(Record):
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
    revision: Annotated[StrictInt, Field(ge=1)]
    source: Source
    frames: Annotated[list[Frame], Field(min_length=1)]
    neurons: list[Neuron]
    observations: list[Observation]

    @model_validator(mode="after")
    def validate_masks_and_references(self) -> Self:
        indices = [frame.frame_index for frame in self.frames]
        if indices != sorted(set(indices)):
            raise ValueError("Frame indices must be unique and sorted")
        times = [frame.timestamp_s for frame in self.frames]
        if any(right <= left for left, right in pairwise(times)):
            raise ValueError("Source timestamps must strictly increase")
        ids = {neuron.id for neuron in self.neurons}
        if len(ids) != len(self.neurons):
            raise ValueError("Duplicate neuron ID")
        if len({neuron.color.lower() for neuron in self.neurons}) != len(ids):
            raise ValueError("Neuron colors must be distinct")
        frames = set(indices)
        pairs: set[tuple[str, int]] = set()
        for observation in self.observations:
            pair = observation.neuron_id, observation.frame_index
            if pair in pairs or pair[0] not in ids or pair[1] not in frames:
                raise ValueError(f"Duplicate or unreferenced observation: {pair}")
            pairs.add(pair)
            intervals: list[Run] = []
            for part, mask in observation.iter_masks():
                previous: Run | None = None
                for run in mask:
                    y, start, end = run
                    if not (0 <= y < self.source.height and 0 <= start < end <= self.source.width):
                        raise ValueError(f"Out-of-bounds pixel run in {pair}, {part}: {run}")
                    if previous is not None:
                        py, _, pend = previous
                        if y < py or (y == py and start <= pend):
                            raise ValueError(f"Runs must be sorted, disjoint and coalesced in {pair}, {part}")
                    previous = run
                intervals.extend(mask)
            intervals.sort()
            for left, right in pairwise(intervals):
                if left[0] == right[0] and right[1] < left[2]:
                    raise ValueError(f"Body, branches and uncertainty overlap in {pair}")
            if observation.visibility in (Visibility.NOT_VISIBLE, Visibility.OUT_OF_FRAME) and intervals:
                raise ValueError(f"Absent observation has pixels: {pair}")
        if len(pairs) != len(ids) * len(frames):
            raise ValueError("Need one observation per neuron per included frame")
        return self
