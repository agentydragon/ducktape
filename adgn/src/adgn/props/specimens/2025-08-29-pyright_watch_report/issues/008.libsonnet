local I = import '../../specimens/lib.libsonnet';

// iss-008: Normalize patterns in one place

I.issueOneOccurrence(
  rationale=|||
    Specimen has many scattered calls to `normalize_pattern`; internal variables are a mix of normalized/un-normalized patterns:

    Original (normalizes per call inside matcher):
    ```python
    def matches_any(path_rel: str, patterns: Iterable[str]) -> bool:
      return any(
          fnmatch.fnmatch(path_rel, normalize_pattern(p))
          or fnmatch.fnmatch("/" + path_rel, normalize_pattern(p))
          for p in patterns
      )
    ```

    Avoid calling `normalize_pattern` in every match - this scatters the responsibility for "patterns should be normalized" all over the code.
    Instead, pass input un-normalized patterns (both include/exclude) *exactly once* through a normalization boundary, and after it, consistently deal only with normalized patterns:

    Better (normalize once; matcher assumes normalized):
    ```python
    include = expand_include_patterns(include)  # returns normalized
    exclude = [normalize_pattern(p) for p in exclude]
    def matches_any(path_rel: str, patterns: Iterable[str]) -> bool:
      return any(
          fnmatch.fnmatch(path_rel, p) or fnmatch.fnmatch("/" + path_rel, p)
          for p in patterns
      )
    ```

    Supplemental: Options for adding more clarity which code assumes normalized / un-normalized patterns:
    * Document normalization requirements/contracts in docstrings/comments
    * Name variable hints, e.g. 'xyzzy_normalized` prefix/suffix
    * Marker type like `NormalizedPattern = NewType("NormalizedPattern", str)`
  |||,
  properties=['python/modern-python-idioms'],
  filesToRanges={
    'pyright_watch_report.py': [65, 70],
  },
  gap_note=|||
    There's analogous gaps in other places about things like clear contract boundary layers / gates (e.g. compiler: 'lex -> AST -> compile pass 1 -> codegen -> optimize -> emit'. Code belongs in clear phases with clear input/output contracts that never or very rarely mix/punch through. 'fn foo(raw input string, piece of AST, 3x piece of Assembly, another raw input string, commandline argv array)' is inherently very suspicious.)
  |||,
)
