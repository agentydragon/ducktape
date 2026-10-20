# Example clip inspection

Inspected on `2026-10-19`. Media is stored in `~/drive/neuron-tracking/data/` and is not included in Git.

| Input                           | SHA-256                                                            | Observed metadata                                                                     |
| ------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| `IMG_6128.AVI`                  | `d3914ca0e68c0d63e1759421529d15f2177747fdc978ba59ec0dba9e6b21c0e3` | H.264, `1280 × 944`, 73 decoded frames, `36.4 s`, 13,265,074 bytes.                   |
| `photo_2026-10-19_14-30-28.jpg` | `61d6be45767abdcd4e549212638bdfdcd5d83fca4285f8ccc99c4184b0a9e0a2` | `1280 × 915`; red outline indicates the region containing small dark target features. |

## Observations

- Every decoded frame was viewed in chronological contact sheets. Decoded frames
  `0`, `36`, and `72` were also viewed at full resolution. This was scene
  inspection, not per-neuron annotation or a tracking accuracy assessment.
- The specimen begins diagonally oriented, becomes approximately horizontal,
  returns to a diagonal pose, then becomes nearly vertical. There are abrupt
  pose/position changes as well as smaller movement within each pose.
- A prominent large dark round feature is spatially distinct from the smaller,
  crowded dark features indicated in the reference image. The annotation
  experiment should target the latter.
- Small-feature contrast and sharpness vary. Some structures appear as connected
  dark patches; this inspection does not establish how many individual neurons
  can be resolved within each patch.
- The video is approximately `2 fps`, but source presentation timestamps begin
  `0.0`, `0.4`, `0.9`, `1.4`, ... and end at `35.9 s`. Preserve those timestamps;
  do not replace them with `frame_index / 2`. The container duration includes the
  final frame's display interval.
- A raw image-space nearest-neighbor assignment would have to contend with the
  large pose changes. Cross-frame identity needs examination of the surrounding
  feature pattern, not just proximity in the full image.

## Reproduce frame extraction

Run from the repository root with inputs under `~/drive/neuron-tracking/data/`:

```bash
mkdir -p "$HOME/drive/neuron-tracking/runs/inspection/frames"
sha256sum "$HOME/drive/neuron-tracking/data/IMG_6128.AVI"
ffprobe -v error -count_frames -select_streams v:0 \
  -show_entries stream=codec_name,width,height,nb_read_frames:format=duration,size \
  -of json "$HOME/drive/neuron-tracking/data/IMG_6128.AVI"
ffprobe -v error -select_streams v:0 \
  -show_entries frame=best_effort_timestamp_time -of csv=p=0 \
  "$HOME/drive/neuron-tracking/data/IMG_6128.AVI" \
  > "$HOME/drive/neuron-tracking/runs/inspection/timestamps.csv"
ffmpeg -hide_banner -loglevel error \
  -i "$HOME/drive/neuron-tracking/data/IMG_6128.AVI" \
  "$HOME/drive/neuron-tracking/runs/inspection/frames/frame-%03d.png"
```

These extraction operations were exercised against the original files with a
temporary output directory. The PNG filename sequence starts at `001`; its
corresponding annotation frame index starts at `0`. The raw `ffprobe` CSV can
include side-data separators; the planned extraction helper should normalize
timestamps into an explicit manifest.
