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

| Feature               | Markdown  | AsciiDoc |    rST    | Org Mode | Typst  | Djot  |
| --------------------- | :-------: | :------: | :-------: | :------: | :----: | :---: |
| Native tables         |   Basic   |   Rich   |   Rich    |   Rich   |  Rich  | Basic |
| Footnotes             | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✓   |
| Admonitions/callouts  | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✗   |
| Cross-references      |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✓   |
| File includes         |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✗   |
| Math (LaTeX)          | Extension |    ✓     | Extension |    ✓     | Native |   ✓   |
| Definition lists      | Extension |    ✓     |     ✓     |    ✓     |   ✗    |   ✓   |
| Auto TOC              | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✗   |
| Attributes/classes    | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✓   |
| Variables/macros      |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✗   |
| Image captions        |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✗   |
| Syntax unambiguous    |     ✗     |    ✓     |     ✓     |    ✓     |   ✓    |   ✓   |
| Task lists            |  ✓ (GFM)  |    ✓     |     ✗     |    ✓     |   ✗    |   ✓   |
| Superscript/subscript | Extension |    ✓     |     ✓     |    ✓     |   ✓    |   ✓   |

Legend: ✓ = native support, ✗ = not supported, Extension = requires extension/flavor
