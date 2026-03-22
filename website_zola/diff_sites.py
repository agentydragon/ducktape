"""Compare Hakyll and Zola static site outputs with DOM-aware HTML normalization.

Produces an HTML report with side-by-side diffs and inline change highlighting.
"""

import difflib
import filecmp
import html
import sys
from dataclasses import dataclass, field
from pathlib import Path

import html5lib
import jinja2


def hakyll_to_zola_path(rel: str) -> str | None:
    """Map a Hakyll relative path to the expected Zola relative path.

    Returns None for files that should be skipped (source files not in output).
    """
    match rel:
        case "about.html":
            return "about/index.html"
        case "found.html":
            return "found/index.html"
        case "nfc.html":
            return "nfc/index.html"
        case "nfc-armband.html":
            return "nfc-armband/index.html"
        case "archive.html":
            return "archive/index.html"
        case "index.html" | "atom.xml" | "rss.xml" | "robots.txt" | "sitemap.xml":
            return rel
        case "css/default.css":
            return "default.css"
        case "css/default.scss":
            return None
        case _ if rel.startswith("posts/") and rel.endswith(".html"):
            return rel.removesuffix(".html") + "/index.html"
        case _:
            return rel


def normalize_html(content: str) -> str:
    """Parse HTML with html5lib (WHATWG spec) and re-serialize canonically."""
    doc = html5lib.parse(content, treebuilder="etree", namespaceHTMLElements=False)
    serialized = html5lib.serialize(
        doc, tree="etree", quote_attr_values="always", omit_optional_tags=False, alphabetical_attributes=True
    )
    return "\n".join(line.strip() for line in serialized.splitlines())


def normalize_text(hakyll_text: str, zola_text: str, is_html: bool) -> tuple[list[str], list[str]]:
    """Normalize and split into lines for comparison."""
    if is_html:
        hakyll_text = normalize_html(hakyll_text)
        zola_text = normalize_html(zola_text)
    return hakyll_text.splitlines(), zola_text.splitlines()


# --- Inline diff rendering ---


def render_inline_diff(old_line: str, new_line: str) -> tuple[str, str]:
    """Render two lines with character-level change highlighting."""
    sm = difflib.SequenceMatcher(None, old_line, new_line)
    old_parts: list[str] = []
    new_parts: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        old_chunk = html.escape(old_line[i1:i2])
        new_chunk = html.escape(new_line[j1:j2])
        if tag == "equal":
            old_parts.append(old_chunk)
            new_parts.append(new_chunk)
        elif tag == "replace":
            old_parts.append(f'<span class="del">{old_chunk}</span>')
            new_parts.append(f'<span class="ins">{new_chunk}</span>')
        elif tag == "delete":
            old_parts.append(f'<span class="del">{old_chunk}</span>')
        elif tag == "insert":
            new_parts.append(f'<span class="ins">{new_chunk}</span>')
    return "".join(old_parts), "".join(new_parts)


@dataclass
class DiffRow:
    kind: str  # "eq", "chg", "del", "ins"
    old_lineno: str
    old_content: str
    new_lineno: str
    new_content: str


def build_diff_rows(old_lines: list[str], new_lines: list[str]) -> list[DiffRow]:
    """Build diff rows for the template."""
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    rows: list[DiffRow] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                esc = html.escape(old_lines[i])
                rows.append(DiffRow("eq", str(i + 1), esc, str(j + 1), esc))
        elif tag == "replace":
            pairs = list(zip(range(i1, i2), range(j1, j2), strict=False))
            for i, j in pairs:
                old_rendered, new_rendered = render_inline_diff(old_lines[i], new_lines[j])
                rows.append(DiffRow("chg", str(i + 1), old_rendered, str(j + 1), new_rendered))
            for i in range(i1 + len(pairs), i2):
                rows.append(DiffRow("del", str(i + 1), html.escape(old_lines[i]), "", ""))
            for j in range(j1 + len(pairs), j2):
                rows.append(DiffRow("ins", "", "", str(j + 1), html.escape(new_lines[j])))
        elif tag == "delete":
            for i in range(i1, i2):
                rows.append(DiffRow("del", str(i + 1), html.escape(old_lines[i]), "", ""))
        elif tag == "insert":
            for j in range(j1, j2):
                rows.append(DiffRow("ins", "", "", str(j + 1), html.escape(new_lines[j])))

    return rows


# --- Report data model ---


@dataclass
class FileEntry:
    rel: str
    zola_rel: str
    status: str  # "identical", "different", "hakyll-only", "zola-only"
    anchor: str
    diff_rows: list[DiffRow] = field(default_factory=list)


REPORT_TEMPLATE = jinja2.Template("""\
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Site Diff: Hakyll vs Zola</title>
<style>
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 1em; background: #fafafa; }
h1 { font-size: 1.4em; }
h2 { font-size: 1.1em; margin-top: 2em; }
.summary { display: flex; gap: 2em; margin: 1em 0; flex-wrap: wrap; }
.summary .stat { padding: 0.5em 1em; border-radius: 4px; font-weight: bold; }
.stat.identical { background: #d4edda; color: #155724; }
.stat.different { background: #f8d7da; color: #721c24; }
.stat.hakyll-only { background: #fff3cd; color: #856404; }
.stat.zola-only { background: #d1ecf1; color: #0c5460; }
details { margin: 0.3em 0; }
summary { cursor: pointer; font-family: monospace; padding: 0.3em; font-size: 13px; }
summary.identical { color: #155724; }
summary.different { color: #721c24; font-weight: bold; }
summary.hakyll-only { color: #856404; }
summary.zola-only { color: #0c5460; }
table.diff { border-collapse: collapse; width: 100%; font-size: 12px; table-layout: fixed; }
table.diff thead th {
  background: #e9ecef; padding: 4px 8px; text-align: left;
  border: 1px solid #ccc; position: sticky; top: 0; z-index: 1;
}
table.diff td {
  padding: 1px 6px; border: 1px solid #eee; vertical-align: top;
  white-space: pre-wrap; word-break: break-all; overflow-wrap: break-word;
}
table.diff td.ln { width: 3em; color: #999; text-align: right; user-select: none; }
table.diff td code { font-size: 11px; }
tr.eq td { background: #fff; }
tr.chg td { background: #fefce8; }
tr.del td { background: #ffeef0; }
tr.ins td { background: #e6ffec; }
span.del { background: #fdb8c0; border-radius: 2px; }
span.ins { background: #acf2bd; border-radius: 2px; }
.toc { column-count: 3; font-family: monospace; font-size: 13px; margin: 1em 0; }
.toc a { text-decoration: none; }
.toc a:hover { text-decoration: underline; }
.toc a.identical { color: #155724; }
.toc a.different { color: #721c24; font-weight: bold; }
.toc a.hakyll-only { color: #856404; }
.toc a.zola-only { color: #0c5460; }
</style>
</head>
<body>
<h1>Site Diff: Hakyll vs Zola</h1>

<div class="summary">
  <span class="stat identical">Identical: {{ counts.identical }}</span>
  <span class="stat different">Different: {{ counts.different }}</span>
  <span class="stat hakyll-only">Hakyll-only: {{ counts.hakyll_only }}</span>
  <span class="stat zola-only">Zola-only: {{ counts.zola_only }}</span>
</div>

<h2>Table of Contents</h2>
<div class="toc">
{% for entry in entries %}
  <div><a class="{{ entry.status }}" href="#{{ entry.anchor }}">{{ entry.rel }}{% if entry.status == 'different' %} ↔ {{ entry.zola_rel }}{% endif %}</a></div>
{% endfor %}
</div>

<h2>Files</h2>
{% for entry in entries %}
<details id="{{ entry.anchor }}"{% if entry.status == 'different' %} open{% endif %}>
  <summary class="{{ entry.status }}">
    {{ entry.status | upper }}  {{ entry.rel }}
    {%- if entry.status == 'different' %}  ↔  {{ entry.zola_rel }}{% endif %}
    {%- if entry.status == 'hakyll-only' %}  (expected: {{ entry.zola_rel }}){% endif %}
  </summary>
  {% if entry.diff_rows %}
  <table class="diff">
    <thead><tr>
      <th colspan="2">hakyll/{{ entry.rel }}</th>
      <th colspan="2">zola/{{ entry.zola_rel }}</th>
    </tr></thead>
    <tbody>
    {% for row in entry.diff_rows %}
      <tr class="{{ row.kind }}">
        <td class="ln">{{ row.old_lineno }}</td><td><code>{{ row.old_content }}</code></td>
        <td class="ln">{{ row.new_lineno }}</td><td><code>{{ row.new_content }}</code></td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</details>
{% endfor %}

</body>
</html>
""")


def main() -> None:
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <hakyll_dir> <zola_dir> <output.html>", file=sys.stderr)
        sys.exit(2)

    hakyll_dir = Path(sys.argv[1])
    zola_dir = Path(sys.argv[2])
    output_path = Path(sys.argv[3])

    entries: list[FileEntry] = []
    expected_zola_paths: set[str] = set()

    for hakyll_file in sorted(hakyll_dir.rglob("*")):
        if not hakyll_file.is_file():
            continue

        rel = str(hakyll_file.relative_to(hakyll_dir))
        zola_rel = hakyll_to_zola_path(rel)
        if zola_rel is None:
            continue

        anchor = rel.replace("/", "-").replace(".", "-")
        expected_zola_paths.add(zola_rel)
        zola_file = zola_dir / zola_rel

        if not zola_file.exists():
            entries.append(FileEntry(rel=rel, zola_rel=zola_rel, status="hakyll-only", anchor=anchor))
            continue

        suffix = hakyll_file.suffix
        is_html = suffix == ".html"
        is_text = suffix in {".html", ".xml", ".css", ".txt"}

        if is_text:
            old_lines, new_lines = normalize_text(hakyll_file.read_text(), zola_file.read_text(), is_html)
            if old_lines == new_lines:
                entries.append(FileEntry(rel=rel, zola_rel=zola_rel, status="identical", anchor=anchor))
            else:
                rows = build_diff_rows(old_lines, new_lines)
                entries.append(FileEntry(rel=rel, zola_rel=zola_rel, status="different", anchor=anchor, diff_rows=rows))
        elif filecmp.cmp(hakyll_file, zola_file, shallow=False):
            entries.append(FileEntry(rel=rel, zola_rel=zola_rel, status="identical", anchor=anchor))
        else:
            entries.append(FileEntry(rel=rel, zola_rel=zola_rel, status="different", anchor=anchor))

    for zola_file in sorted(zola_dir.rglob("*")):
        if not zola_file.is_file():
            continue
        rel = str(zola_file.relative_to(zola_dir))
        if rel not in expected_zola_paths:
            anchor = rel.replace("/", "-").replace(".", "-")
            entries.append(FileEntry(rel=rel, zola_rel=rel, status="zola-only", anchor=anchor))

    counts = {
        "identical": sum(1 for e in entries if e.status == "identical"),
        "different": sum(1 for e in entries if e.status == "different"),
        "hakyll_only": sum(1 for e in entries if e.status == "hakyll-only"),
        "zola_only": sum(1 for e in entries if e.status == "zola-only"),
    }

    report_html = REPORT_TEMPLATE.render(entries=entries, counts=counts)
    output_path.write_text(report_html)
    print(f"Report: {output_path}")
    print(
        f"  Identical: {counts['identical']}  Different: {counts['different']}  Hakyll-only: {counts['hakyll_only']}  Zola-only: {counts['zola_only']}"
    )


if __name__ == "__main__":
    main()
