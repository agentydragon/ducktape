// Typst Example
// Compile with: typst compile example.typ

#set document(title: "Typst Example", author: "Author Name")
#set page(numbering: "1")

#outline()

= Text Formatting

This is *bold* and this is _italic_.

Superscript: E=mc#super[2] and subscript: H#sub[2]O

= Lists

- Unordered item
- Another item
  - Nested item

+ First (numbered)
+ Second

/ Term 1: Definition of term 1
/ Term 2: Definition of term 2

= Links and Images

#link("https://example.com")[Link text]

#image("image.png", width: 50%)

#figure(
  image("diagram.png", width: 70%),
  caption: [Image with caption],
)

= Code

Inline `code` and blocks:

```python
def hello():
    print("Hello, world!")
```

= Tables

#table(
  columns: (auto, auto),
  [*Name*], [*Value*],
  [Alpha], [1],
  [Beta], [2],
)

#figure(
  table(
    columns: 3,
    [*A*], [*B*], [*C*],
    [1], [2], [3],
    table.cell(colspan: 2)[spans two], [3],
  ),
  caption: [Table with caption and column span],
)

= Admonitions (via custom blocks)

#block(
  fill: luma(230),
  inset: 8pt,
  radius: 4pt,
  [*Note:* This is a note.],
)

#block(
  fill: rgb("#fff3cd"),
  inset: 8pt,
  radius: 4pt,
  [*Warning:* This is a warning.],
)

= Cross-references

See @tables for table examples.

== Custom Anchor Section <custom-anchor>

Reference with @custom-anchor.

= Includes

#include "other-file.typ"

= Variables

#let project-name = "MyProject"

The project is called #project-name.

= Footnotes

Here's a sentence with a footnote#footnote[This is the footnote content.].

= Blockquotes

#quote(attribution: [Albert Einstein])[
  Imagination is more important than knowledge.
]

= Math (Native)

Inline: $E = m c^2$

Block:

$ integral_0^infinity e^(-x^2) dif x = sqrt(pi) / 2 $

= Typst-specific Features

== Functions and Logic

#let greet(name) = [Hello, #name!]

#greet("World")

== Conditionals

#let score = 85
#if score >= 90 [Excellent!] else if score >= 70 [Good!] else [Keep trying!]

== Loops

#for i in range(1, 4) [
  Item #i
]

== Styling

#text(fill: red)[Red text]
#text(size: 1.5em)[Larger text]
#text(font: "Courier New")[Monospace]

== Layout

#grid(
  columns: (1fr, 1fr),
  gutter: 1em,
  [Left column],
  [Right column],
)
