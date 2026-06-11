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

## Task Lists (GFM extension)

- [x] Completed task
- [ ] Pending task

## Blockquotes

> This is a quote.
> It can span multiple lines.

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
