# Markdown Example

## Text Formatting

This is **bold** and this is _italic_. You can also do ~~strikethrough~~.

## Lists

- Unordered item
- Another item
  - Nested item

1. First
2. Second

## Links and Images

[Link text](https://example.com)

![Alt text](image.png)

## Code

Inline `code` and blocks:

```python
def hello():
    print("Hello, world!")
```

## Tables (GFM extension)

| Name  | Value |
| ----- | ----- |
| Alpha | 1     |
| Beta  | 2     |

## Definition Lists (Markdown Extra extension)

Term 1
: Definition of term 1

Term 2
: Definition of term 2

## Auto TOC (GitLab extension)

[[_TOC_]]

## Attributes and Classes (Pandoc extension)

```text
[This paragraph has a class.]{.warning}
```

## Task Lists (GFM extension)

- [x] Completed task
- [ ] Pending task

## Blockquotes

> This is a quote.
> It can span multiple lines.

## Admonitions (GitHub extension)

> [!NOTE]
> This is a note.

## Math (extension)

Inline: $E = mc^2$

Block:

$$
\int_0^\infty e^{-x^2} dx = \frac{\sqrt{\pi}}{2}
$$

## Superscript and Subscript (Pandoc extension)

```text
E=mc^2^ and H~2~O
```

## Footnotes (extension, not CommonMark)

Here's a sentence with a footnote[^1].

[^1]: This is the footnote content.

## Horizontal Rule

---

## Limitations

- No native admonitions (need HTML or extensions)
- No file includes
- No cross-references
- Table syntax is limited (no spanning, no alignment control)
- Ambiguous parsing in edge cases
