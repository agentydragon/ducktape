# Frontend primitives

`Markdown` and `CodeBlock` render untrusted prose and read-only code.
`Disclosure`, `DisclosureCard`, `DisclosureBody` and `ClampedBlock` coordinate
nested headings and expandable content. These primitives also serve non-transcript
views, alongside tooltips and resizable panes.

The [transcript package](transcript/README.md) composes these primitives into message
presentation and virtualized history, with retained expansion state, scrolling,
geometry and diagnostics. Applications supply their own decoding and grouping policies.
