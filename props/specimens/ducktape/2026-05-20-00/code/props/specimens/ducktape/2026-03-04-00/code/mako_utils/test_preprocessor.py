import pytest_bazel
from mako.template import Template

from mako_utils.preprocessor import markdown_heading_preprocessor


def test_h2_preserved():
    src = "## Heading\nBody text"
    result = Template(src, preprocessor=markdown_heading_preprocessor).render()
    assert result == "## Heading\nBody text"


def test_h3_preserved():
    src = "### Sub-heading"
    result = Template(src, preprocessor=markdown_heading_preprocessor).render()
    assert result == "### Sub-heading"


def test_h4_preserved():
    src = "#### Deep heading"
    result = Template(src, preprocessor=markdown_heading_preprocessor).render()
    assert result == "#### Deep heading"


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


def test_hashes_in_code_block_preserved():
    """Hashes inside template expressions should not be double-escaped."""
    src = "${header}\nBody"
    result = Template(src, preprocessor=markdown_heading_preprocessor).render(header="## Dynamic")
    assert result == "## Dynamic\nBody"


if __name__ == "__main__":
    pytest_bazel.main()
