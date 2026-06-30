import pytest
import pytest_bazel
from fastmcp.exceptions import ToolError

from haku.gmail_labeling.namespace import LabelNamespace


def test_allows_only_under_prefix():
    ns = LabelNamespace("haku/")
    assert ns.allows("haku/triaged")
    assert ns.allows("haku/")
    # The bare folder name (no trailing slash) is a sibling, not inside.
    assert not ns.allows("haku")
    assert not ns.allows("Important")
    assert not ns.allows("INBOX")


def test_require_passes_inside_raises_outside():
    ns = LabelNamespace("haku/")
    ns.require("haku/x")  # no raise
    with pytest.raises(ToolError):
        ns.require("INBOX")


def test_empty_prefix_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        LabelNamespace("")


if __name__ == "__main__":
    pytest_bazel.main()
