"""Synthetic pixels exercise rendering, identity colors, and media round trips."""

import json
from fractions import Fraction
from pathlib import Path

import av
import jsonschema
import pytest
import pytest_bazel
from PIL import Image
from pydantic import ValidationError

from x.neuron_tracking.annotations import Annotations, Frame, Neuron, Observation, Review, Source, Visibility
from x.neuron_tracking.render import Layer, RenderReport, hash_file, render, render_frame


def make_annotations(source: Path, timestamps: list[float]) -> Annotations:
    observations = []
    for index in range(len(timestamps)):
        for identity, x, y in (("N001", 20 if index % 2 == 0 else 80, 30), ("N002", 80 if index % 2 == 0 else 20, 60)):
            observations.append(
                Observation(
                    neuron_id=identity,
                    frame_index=index,
                    visibility=Visibility.VISIBLE,
                    body=[(y, x, x + 3)],
                    branches=[(y + 1, x, x + 4)],
                    uncertain=[(y + 2, x, x + 8)],
                    review=Review.PROPOSED,
                )
            )
    return Annotations(
        schema_version=1,
        revision=7,
        source=Source(path=str(source), sha256=hash_file(source), width=128, height=96),
        frames=[Frame(frame_index=i, timestamp_s=t) for i, t in enumerate(timestamps)],
        neurons=[Neuron(id="N001", color="#ff0000"), Neuron(id="N002", color="#0000ff")],
        observations=observations,
    )


@pytest.fixture
def sample(tmp_path: Path) -> tuple[Path, Path, Annotations]:
    source = tmp_path / "source.mkv"
    with av.open(str(source), mode="w") as container:
        stream = container.add_stream("ffv1", rate=2)
        stream.width = 128
        stream.height = 96
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, 1000)
        stream.codec_context.time_base = Fraction(1, 1000)
        for timestamp in (0, 400, 900):
            frame = av.VideoFrame.from_image(Image.new("RGB", (128, 96), (100, 100, 100)))
            frame.pts = timestamp
            frame.time_base = Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    with av.open(str(source)) as container:
        timestamps = [frame.time for frame in container.decode(video=0)]
    assert timestamps == [0, 0.4, 0.9]
    annotations = make_annotations(source, [float(t) for t in timestamps if t is not None])
    path = tmp_path / "annotations.json"
    path.write_text(annotations.model_dump_json())
    return source, path, annotations


def test_pixels_layers_and_identity_colors(sample: tuple[Path, Path, Annotations]) -> None:
    _, _, annotations = sample
    raw = Image.new("RGB", (128, 96), (100, 100, 100))
    for index in (0, 1):
        combined = render_frame(
            raw, annotations, index, layer=Layer.COMBINED, neuron_ids={"N001", "N002"}, labels=False
        )
        red_x, blue_x = (20, 80) if index == 0 else (80, 20)
        red = combined.getpixel((red_x, 30))
        blue = combined.getpixel((blue_x, 60))
        assert isinstance(red, tuple)
        assert isinstance(blue, tuple)
        assert red[0] > red[1] == red[2]
        assert blue[2] > blue[0] == blue[1]
        assert combined.getpixel((red_x + 3, 30)) == raw.getpixel((red_x + 3, 30))
        assert combined.getpixel((red_x + 3, 31)) != raw.getpixel((red_x + 3, 31))
        assert len({combined.getpixel((red_x + offset, 32)) for offset in range(8)}) == 2
    body = render_frame(raw, annotations, 0, layer=Layer.BODY, neuron_ids={"N001"}, labels=False)
    assert body.getpixel((20, 30)) != raw.getpixel((20, 30))
    assert body.getpixel((20, 31)) == raw.getpixel((20, 31))
    assert body.getpixel((80, 60)) == raw.getpixel((80, 60))


def test_serialized_annotations_match_published_schema(sample: tuple[Path, Path, Annotations]) -> None:
    _, _, annotations = sample
    schema = json.loads(Path(__file__).with_name("annotations.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(annotations.model_dump(mode="json"))


def test_png_matches_lossless_avi_and_timing_is_reported(
    sample: tuple[Path, Path, Annotations], tmp_path: Path
) -> None:
    source, path, annotations = sample
    png = tmp_path / "frame.png"
    still = render(source, path, png, frame_index=1, layer=Layer.COMBINED, neuron_ids=None, labels=True)
    avi = tmp_path / "annotated.avi"
    report = render(source, path, avi, frame_index=None, layer=Layer.COMBINED, neuron_ids=None, labels=True)
    with av.open(str(avi)) as container:
        images = [frame.to_image() for frame in container.decode(video=0)]
    assert len(images) == 3
    assert all(image.size == (128, 96) for image in images)
    with Image.open(png) as image:
        assert images[1].tobytes() == image.tobytes()
    assert still.neuron_colors == report.neuron_colors == {"N001": "#ff0000", "N002": "#0000ff"}
    assert report.frame_indices == [0, 1, 2]
    assert report.source_timestamps_s == [0, 0.4, 0.9]
    assert report.output_timestamps_s == pytest.approx([0, 7 / 15, 14 / 15])
    assert report.source_duration_s == pytest.approx(1.4)
    assert report.output_duration_s == pytest.approx(1.4)
    assert report.output_sha256 == hash_file(avi)
    assert report.annotation_sha256 == hash_file(path)
    saved = RenderReport.model_validate_json(avi.with_suffix(".avi.json").read_text())
    assert saved == report
    assert annotations == Annotations.model_validate_json(path.read_text())


@pytest.mark.parametrize("failure", ["bounds", "overlap", "touching", "colors", "reference", "missing", "absent"])
def test_invalid_annotations_are_rejected(sample: tuple[Path, Path, Annotations], failure: str) -> None:
    _, _, annotations = sample
    data = annotations.model_dump(mode="json")
    observation = data["observations"][0]
    match failure:
        case "bounds":
            observation["body"] = [[30, 128, 129]]
        case "overlap":
            observation["branches"] = observation["body"]
        case "touching":
            observation["body"] = [[30, 20, 23], [30, 23, 25]]
        case "colors":
            data["neurons"][1]["color"] = "#FF0000"
        case "reference":
            observation["neuron_id"] = "missing"
        case "missing":
            data["observations"].pop()
        case "absent":
            observation["visibility"] = "not_visible"
    with pytest.raises(ValidationError):
        Annotations.model_validate_json(json.dumps(data))


def test_partial_annotation_can_render_anchor_frame(sample: tuple[Path, Path, Annotations], tmp_path: Path) -> None:
    source, path, annotations = sample
    partial = annotations.model_copy(
        update={
            "frames": [annotations.frames[1]],
            "observations": [o for o in annotations.observations if o.frame_index == 1],
        }
    )
    path.write_text(partial.model_dump_json())
    render(
        source, path, tmp_path / "anchor.png", frame_index=1, layer=Layer.BRANCHES, neuron_ids={"N002"}, labels=False
    )
    with pytest.raises(ValueError, match="every source frame"):
        render(
            source, path, tmp_path / "partial.avi", frame_index=None, layer=Layer.COMBINED, neuron_ids=None, labels=True
        )


@pytest.mark.parametrize("failure", ["hash", "timestamp", "overwrite"])
def test_source_mismatch_and_input_overwrite_are_rejected(
    sample: tuple[Path, Path, Annotations], tmp_path: Path, failure: str
) -> None:
    source, path, annotations = sample
    data = annotations.model_dump(mode="json")
    match failure:
        case "hash":
            data["source"]["sha256"] = "0" * 64
        case "timestamp":
            data["frames"][0]["timestamp_s"] = 0.01
        case "overwrite":
            pass
    path.write_text(json.dumps(data))
    output = source if failure == "overwrite" else tmp_path / "bad.avi"
    error = {"hash": "SHA-256", "timestamp": "timestamp", "overwrite": "overwrite"}[failure]
    with pytest.raises(ValueError, match=error):
        render(source, path, output, frame_index=None, layer=Layer.COMBINED, neuron_ids=None, labels=True)
    if failure != "overwrite":
        assert not output.exists()


if __name__ == "__main__":
    pytest_bazel.main()
