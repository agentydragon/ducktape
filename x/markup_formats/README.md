# Lightweight Markup Format Comparison

## Common Features (all formats support these)

- Headings (multiple levels)
- Bold and italic text
- Ordered and unordered lists
- Hyperlinks
- Inline code
- Code blocks
- Paragraphs
- Images (basic)

## Feature Matrix

| Feature                | Markdown  | AsciiDoc |    rST    | Org Mode | Typst  |  Djot  |
| ---------------------- | :-------: | :------: | :-------: | :------: | :----: | :----: |
| Native tables          |   Basic   |   Rich   |   Rich    |   Rich   |  Rich  | Basic  |
| Footnotes              | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✓    |
| Admonitions/callouts   | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✗    |
| Cross-references       |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✓    |
| File includes          |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✗    |
| Math (LaTeX)           | Extension |    ✓     | Extension |    ✓     | Native |   ✓    |
| Definition lists       | Extension |    ✓     |     ✓     |    ✓     |   ✗    |   ✓    |
| Auto TOC               | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✗    |
| Attributes/classes     | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✓    |
| Variables/macros       |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✗    |
| Image captions         |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✗    |
| Path-only file links   |     ✗     |    ✓     |     ✗     |    ✓     |   ✗    |   ✗    |
| GitHub rendered prose  |     ✓     |    ✓     |     ✓     |    ✓     |   ✗    |   ✗    |
| GitLab rendered prose  |     ✓     |    ✓     |     ✓     |    ✓     |   ✗    |   ✗    |
| Forgejo rendered prose |     ✓     |  Config  |  Config   |    ✓     | Config | Config |
| Syntax unambiguous     |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✓    |
| Task lists             |  ✓ (GFM)  |    ✓     |     ✗     |    ✓     |   ✗    |   ✓    |
| Superscript/subscript  | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✓    |

Legend: ✓ = native support, ✗ = not supported, Extension = requires extension/flavor, Config = requires per-instance
renderer configuration

**Path-only file links** use a relative path exactly once and derive the link text from that path. Explicitly labelled
links, such as `[documentation](docs/guide.md)`, are supported by every format but are outside this comparison.
Markdown's `<docs/guide.md>` is plain text, not a relative-file autolink; CommonMark autolinks require a URI scheme.

The hosted-renderer rows describe current repository-file previews. Forgejo's built-in renderers support Markdown and
Org Mode; its administrator can configure an external renderer for the formats marked Config.
