"""Render saved neuron identities and pixel masks over source video frames."""

import argparse
import hashlib
import json
from enum import StrEnum
from fractions import Fraction
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory

import av
from PIL import Image, ImageColor, ImageDraw, ImageFont
from pydantic import BaseModel

from x.neuron_tracking.annotations import Annotations, Mask, Part, mask_center


class Layer(StrEnum):
    COMBINED = "combined"
    BODY = "body"
    BRANCHES = "branches"
    UNCERTAIN = "uncertain"


class RenderReport(BaseModel):
    annotation_sha256: str
    source_sha256: str
    output_sha256: str
    revision: int
    layer: Layer
    labels: bool
    neuron_colors: dict[str, str]
    frame_indices: list[int]
    source_timestamps_s: list[float]
    output_timestamps_s: list[float] | None
    source_duration_s: float | None
    output_duration_s: float | None


def hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def render_frame(
    image: Image.Image, annotations: Annotations, frame_index: int, *, layer: Layer, neuron_ids: set[str], labels: bool
) -> Image.Image:
    if image.size != (annotations.source.width, annotations.source.height):
        raise ValueError("Decoded frame dimensions differ from annotation source")
    colors: dict[str, tuple[int, ...]] = {neuron.id: ImageColor.getrgb(neuron.color) for neuron in annotations.neurons}
    overlay = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(overlay)
    anchors: list[tuple[str, tuple[float, float]]] = []
    observations = sorted(
        (o for o in annotations.observations if o.frame_index == frame_index and o.neuron_id in neuron_ids),
        key=lambda o: o.neuron_id,
    )
    for observation in observations:
        displayed: Mask = []
        color = colors[observation.neuron_id]
        for part, mask in observation.iter_masks():
            if layer != Layer.COMBINED and Part(layer) != part:
                continue
            displayed.extend(mask)
            for y, start, end in mask:
                match part:
                    case Part.BODY:
                        draw.line((start, y, end - 1, y), fill=(*color, 100))
                    case Part.BRANCHES:
                        draw.line((start, y, end - 1, y), fill=(*color, 210))
                    case Part.UNCERTAIN:
                        for x in range(start, end):
                            draw.point((x, y), fill=(*color, 180 if (x + y) % 4 < 2 else 45))
        if (center := mask_center(displayed)) is not None:
            anchors.append((observation.neuron_id, center))
    if labels:
        draw_labels(draw, anchors, colors=colors, size=image.size)
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def draw_labels(
    draw: ImageDraw.ImageDraw,
    anchors: list[tuple[str, tuple[float, float]]],
    *,
    colors: dict[str, tuple[int, ...]],
    size: tuple[int, int],
) -> None:
    font = ImageFont.load_default(size=14)
    occupied: list[tuple[int, int, int, int]] = []
    for identity, (x, y) in anchors:
        left, top, right, bottom = draw.textbbox((0, 0), identity, font=font)
        width, height = ceil(right - left) + 6, ceil(bottom - top) + 6
        if width > size[0] or height > size[1]:
            raise ValueError(f"ID label does not fit frame: {identity!r}; use --no-labels")
        candidates = []
        for distance in (12, 30, 50, 75, 110):
            for dx, dy in ((distance, -height), (distance, distance), (-width - distance, -height), (-width, distance)):
                lx = max(0, min(size[0] - width, round(x + dx)))
                ly = max(0, min(size[1] - height, round(y + dy)))
                rectangle = (lx, ly, lx + width, ly + height)
                collisions = sum(
                    lx < occupied_right
                    and lx + width > occupied_left
                    and ly < occupied_bottom
                    and ly + height > occupied_top
                    for occupied_left, occupied_top, occupied_right, occupied_bottom in occupied
                )
                candidates.append((collisions, rectangle))
        _, rectangle = min(candidates, key=lambda candidate: candidate[0])
        occupied.append(rectangle)
        lx, ly, _, _ = rectangle
        color = colors[identity]
        draw.line((round(x), round(y), lx + width // 2, ly + height // 2), fill=(*color, 255))
        draw.rectangle(rectangle, fill=(0, 0, 0, 210), outline=(*color, 255))
        draw.text((lx + 3 - left, ly + 3 - top), identity, font=font, fill=(255, 255, 255, 255))


def check_frame(frame: av.VideoFrame, expected_timestamp: float, annotations: Annotations) -> None:
    if (frame.width, frame.height) != (annotations.source.width, annotations.source.height):
        raise ValueError("Decoded frame dimensions differ from annotation source")
    if frame.time is None or abs(frame.time - expected_timestamp) > 0.00001:
        raise ValueError(f"Source timestamp differs from annotation: {frame.time} vs {expected_timestamp}")


def render(
    source: Path,
    annotation_path: Path,
    output: Path,
    *,
    frame_index: int | None,
    layer: Layer,
    neuron_ids: set[str] | None,
    labels: bool,
) -> RenderReport:
    annotation_bytes = annotation_path.read_bytes()
    annotations = Annotations.model_validate_json(annotation_bytes)
    if hash_file(source) != annotations.source.sha256:
        raise ValueError("Source SHA-256 differs from annotations")
    report_path = output.with_suffix(output.suffix + ".json")
    if any(path.resolve() in (source.resolve(), annotation_path.resolve()) for path in (output, report_path)):
        raise ValueError("Output would overwrite an input")
    all_ids = {neuron.id for neuron in annotations.neurons}
    selected = all_ids if neuron_ids is None else neuron_ids
    if selected - all_ids:
        raise ValueError(f"Unknown neuron IDs: {sorted(selected - all_ids)}")
    frames = {frame.frame_index: frame.timestamp_s for frame in annotations.frames}
    if frame_index is not None and frame_index not in frames:
        raise ValueError(f"Frame {frame_index} is not included in the annotation JSON")
    if frame_index is None and list(frames) != list(range(len(frames))):
        raise ValueError("AVI export needs annotations for every source frame, starting at zero")
    expected_suffix = ".avi" if frame_index is None else ".png"
    if output.suffix.lower() != expected_suffix:
        raise ValueError(f"Output must use {expected_suffix}")
    output.parent.mkdir(parents=True, exist_ok=True)
    indices = list(frames) if frame_index is None else [frame_index]
    output_times: list[float] | None = None
    source_duration: float | None = None
    output_duration: float | None = None
    with TemporaryDirectory(dir=output.parent, prefix=".neuron-render-") as temporary:
        rendered_path = Path(temporary) / output.name
        if frame_index is not None:
            with av.open(str(source)) as container:
                for index, frame in enumerate(container.decode(video=0)):
                    if index == frame_index:
                        check_frame(frame, frames[index], annotations)
                        render_frame(
                            frame.to_image(), annotations, index, layer=layer, neuron_ids=selected, labels=labels
                        ).save(rendered_path)
                        break
                else:
                    raise ValueError(f"Source has no frame {frame_index}")
        else:
            source_duration = measure_source(source, annotations)
            rate = (Fraction(len(frames)) / Fraction(str(source_duration))).limit_denominator(100000)
            with av.open(str(source)) as input_video, av.open(str(rendered_path), mode="w", format="avi") as encoded:
                stream = encoded.add_stream("ffv1", rate=rate)
                stream.width = annotations.source.width
                stream.height = annotations.source.height
                stream.pix_fmt = "bgr0"
                for index, frame in enumerate(input_video.decode(video=0)):
                    if index not in frames:
                        raise ValueError("Source has more frames than annotations")
                    check_frame(frame, frames[index], annotations)
                    image = render_frame(
                        frame.to_image(), annotations, index, layer=layer, neuron_ids=selected, labels=labels
                    )
                    rendered = av.VideoFrame.from_image(image)
                    rendered.pts = index
                    rendered.time_base = 1 / rate
                    for packet in stream.encode(rendered):
                        encoded.mux(packet)
                for packet in stream.encode():
                    encoded.mux(packet)
            output_times, output_duration = inspect_output(rendered_path, annotations)
        report = RenderReport(
            annotation_sha256=hashlib.sha256(annotation_bytes).hexdigest(),
            source_sha256=annotations.source.sha256,
            output_sha256=hash_file(rendered_path),
            revision=annotations.revision,
            layer=layer,
            labels=labels,
            neuron_colors={neuron.id: neuron.color for neuron in annotations.neurons if neuron.id in selected},
            frame_indices=indices,
            source_timestamps_s=[frames[index] for index in indices],
            output_timestamps_s=output_times,
            source_duration_s=source_duration,
            output_duration_s=output_duration,
        )
        temporary_report = Path(temporary) / report_path.name
        temporary_report.write_text(report.model_dump_json(indent=2) + "\n")
        rendered_path.replace(output)
        temporary_report.replace(report_path)
    return report


def measure_source(source: Path, annotations: Annotations) -> float:
    with av.open(str(source)) as container:
        count = 0
        end: Fraction | None = None
        for frame, expected in zip(container.decode(video=0), annotations.frames, strict=True):
            check_frame(frame, expected.timestamp_s, annotations)
            if frame.pts is None or frame.time_base is None or frame.duration <= 0:
                raise ValueError("Source needs explicit frame timestamps and positive frame durations")
            end = (frame.pts + frame.duration) * frame.time_base
            count += 1
        if count == 0 or end is None:
            raise ValueError("Source contains no video frames")
    return float(end) - annotations.frames[0].timestamp_s


def inspect_output(path: Path, annotations: Annotations) -> tuple[list[float], float]:
    timestamps: list[float] = []
    end: Fraction | None = None
    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            if (frame.width, frame.height) != (annotations.source.width, annotations.source.height):
                raise ValueError("Encoded AVI changed frame dimensions")
            if frame.time is None or frame.pts is None or frame.time_base is None or frame.duration <= 0:
                raise ValueError("Encoded AVI lacks timestamps or frame durations")
            timestamps.append(frame.time)
            end = (frame.pts + frame.duration) * frame.time_base
    if len(timestamps) != len(annotations.frames) or end is None:
        raise ValueError("Encoded AVI changed frame count")
    return timestamps, float(end)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--annotations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frame", type=int, help="Render one frame to PNG; omit to export the entire AVI")
    parser.add_argument("--layer", type=Layer, choices=list(Layer), default=Layer.COMBINED)
    parser.add_argument("--neuron", action="append", help="Render only this ID; repeat to select several")
    parser.add_argument("--no-labels", action="store_true")
    arguments = parser.parse_args()
    report = render(
        arguments.source,
        arguments.annotations,
        arguments.output,
        frame_index=arguments.frame,
        layer=arguments.layer,
        neuron_ids=None if arguments.neuron is None else set(arguments.neuron),
        labels=not arguments.no_labels,
    )
    print(
        json.dumps({"output": str(arguments.output), "revision": report.revision, "frames": len(report.frame_indices)})
    )


if __name__ == "__main__":
    main()
