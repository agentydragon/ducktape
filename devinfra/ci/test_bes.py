import pytest
import pytest_bazel

from devinfra.ci.bes import BuildBuddyError, parse

# Shaped exactly as BuildBuddy serves it. "outer" nests "inner" because Bazel
# shares file subsets between targets instead of repeating their contents.
STREAM = b"""[
 {"id":{"namedSet":{"id":"inner"}},
  "namedSetOfFiles":{"files":[
    {"pathPrefix":["bazel-out","k8-fastbuild","bin"],"name":"skills/backtrace/backtrace.skill",
     "uri":"bytestream://h/blobs/aaa/12","digest":"aaa","length":"12"}]}},
 {"id":{"namedSet":{"id":"outer"}},
  "namedSetOfFiles":{"fileSets":[{"id":"inner"}],"files":[
    {"pathPrefix":["bazel-out","k8-fastbuild","bin"],"name":"aiquota/aiquota.zip",
     "uri":"bytestream://h/blobs/bbb/34","digest":"bbb","length":"34"}]}},
 {"id":{"namedSet":{"id":"clock"}},
  "namedSetOfFiles":{"files":[
    {"name":"util/testing/frozen-clock.js","uri":"bytestream://h/blobs/src/9","digest":"src","length":"9"},
    {"pathPrefix":["bazel-out","k8-fastbuild","bin"],"name":"util/testing/frozen-clock.js",
     "uri":"bytestream://h/blobs/gen/9","digest":"gen","length":"9"}]}},
 {"id":{"targetCompleted":{"label":"//skills/backtrace:backtrace_skill"}},
  "completed":{"success":true,"outputGroup":[{"name":"default","fileSets":[{"id":"outer"}]}]}},
 {"id":{"targetCompleted":{"label":"//util/testing:clock"}},
  "completed":{"success":true,"outputGroup":[{"name":"default","fileSets":[{"id":"clock"}]}]}},
 {"id":{"testSummary":{"label":"//x/study_casino:test_app"}},"testSummary":{"overallStatus":"TIMEOUT"}},
 {"id":{"testSummary":{"label":"//devinfra/ci:test_bes"}},"testSummary":{"overallStatus":"PASSED"}}
]"""


def test_a_nested_file_set_is_walked() -> None:
    """The .skill is only reachable through a second set; missing it would hide
    most outputs of any non-trivial target while still looking like it worked."""
    paths = {o.path for o in parse(STREAM).outputs}
    assert "bazel-out/k8-fastbuild/bin/skills/backtrace/backtrace.skill" in paths


def test_a_file_is_identified_by_prefix_and_name_together() -> None:
    """A source file and the generated file of the same name differ only by prefix,
    so keying on name alone would hand a caller the source bytes."""
    outputs = [o for o in parse(STREAM).outputs if o.path.endswith("util/testing/frozen-clock.js")]
    assert {o.digest for o in outputs} == {"src", "gen"}
    assert parse(STREAM).by_path()["util/testing/frozen-clock.js"].digest == "src"
    assert parse(STREAM).by_path()["bazel-out/k8-fastbuild/bin/util/testing/frozen-clock.js"].digest == "gen"


def test_outputs_carry_the_label_that_produced_them() -> None:
    by_path = parse(STREAM).by_path()
    assert by_path["bazel-out/k8-fastbuild/bin/aiquota/aiquota.zip"].label == "//skills/backtrace:backtrace_skill"


def test_outputs_can_be_found_by_label() -> None:
    """A label is the only reliable handle: an external repo's directory name is
    mangled by bzlmod, and most image targets share the basename `image`."""
    grouped = parse(STREAM).by_label()
    assert {o.path for o in grouped["//util/testing:clock"]} == {
        "util/testing/frozen-clock.js",
        "bazel-out/k8-fastbuild/bin/util/testing/frozen-clock.js",
    }


def test_outputs_carry_the_uri_needed_to_fetch_them() -> None:
    """An image's digest is the *contents* of its file, not the file's own digest."""
    assert (
        parse(STREAM)
        .by_path()["bazel-out/k8-fastbuild/bin/skills/backtrace/backtrace.skill"]
        .uri.startswith("bytestream://")
    )


def test_size_survives_being_a_json_string() -> None:
    """BES encodes int64 as a string; decoding it as one would break comparisons."""
    assert parse(STREAM).by_path()["bazel-out/k8-fastbuild/bin/aiquota/aiquota.zip"].size == 34


def test_test_verdicts_come_from_the_same_stream() -> None:
    """This is what lets a publish gate consult the build instead of re-running it."""
    assert parse(STREAM).test_status == {"//x/study_casino:test_app": "TIMEOUT", "//devinfra/ci:test_bes": "PASSED"}


def test_an_unusable_stream_raises_rather_than_reading_as_empty() -> None:
    """Empty would mean "nothing was built", which a planner reads as "nothing changed"."""
    with pytest.raises(BuildBuddyError):
        parse(b"<html>502 Bad Gateway</html>")
    with pytest.raises(BuildBuddyError):
        parse(b'{"not": "a list"}')


if __name__ == "__main__":
    pytest_bazel.main()
