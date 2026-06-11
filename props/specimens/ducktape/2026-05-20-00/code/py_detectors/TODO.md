# Detector Ideas

## Proposed Detectors

- **str_conversion_to_internal_api**: `str(path)` passed to a non-stdlib function.
  Unlike `pathlike_str_casts` (which flags `str(path)` to known stdlib PathLike APIs),
  this would flag conversions to internal/project functions, signaling potential API
  improvement - should the callee accept `Path` directly?
  Heuristic: Flag `str(x)` as argument where `x` has a name containing `path`, `file`, `dir`.
  High FP rate expected; may need allowlist or confidence scoring.
