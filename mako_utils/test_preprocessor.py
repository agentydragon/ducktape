import pytest_bazel
from mako.template import Template

from mako_utils.preprocessor import markdown_heading_preprocessor


def test_single_hash_untouched():
    """Single # is not a Mako comment, should pass through unmodified."""
    src = "# Top heading"
    result = Template(src, preprocessor=markdown_heading_preprocessor).render()
    assert result == "# Top heading"


def test_mako_expressions_still_work():
    src = "## ${name}\nHello"
    result = Template(src, preprocessor=markdown_heading_preprocessor).render(name="World")
    assert result == "## World\nHello"


def test_multiple_headings():
    src = "## First\n\nSome text\n\n### Second\n\nMore text\n\n#### Third"
    result = Template(src, preprocessor=markdown_heading_preprocessor).render()
    assert result == "## First\n\nSome text\n\n### Second\n\nMore text\n\n#### Third"


if __name__ == "__main__":
    pytest_bazel.main()
