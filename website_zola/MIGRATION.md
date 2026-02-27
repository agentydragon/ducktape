# Hakyll → Zola Migration Notes

## Why

The Hakyll-based website (`website/`) cannot build on NixOS: GHC 9.0.2 bindist's
`./configure` fails because NixOS lacks standard ELF interpreter paths. This also
blocks local Bazel builds with RBE, since repository rules (GHC bindist setup)
run locally even with `--remote_executor`. BuildBuddy Workflows CI would work
(Ubuntu runners), but local development is broken.

Zola is a single static binary (Rust) with no runtime deps. It builds on NixOS
natively via nixpkgs, and the release binary works on RBE Ubuntu workers. The
Bazel integration is a simple `http_archive` + `genrule` — no `rules_haskell`,
no GHC, no Stackage.

## URL Compatibility

**Problem**: Hakyll produces flat `.html` files (`/posts/2023-07-04-con-folder.html`).
Zola always produces directory-style URLs (`/posts/2023-07-04-con-folder/index.html`).
There is no Zola configuration to produce flat `.html` files.

**Options considered**:

1. **nginx rewrite rules** (chosen for deployment): Rewrite `/posts/*.html` →
   `/posts/*/` so old bookmarks and search engine links continue to work. This
   is a server-side concern and doesn't affect the Zola site itself.

2. **Zola `aliases`**: Each page can declare `aliases = ["/posts/2023-07-04-con-folder.html"]`
   which generates a redirect HTML file at the old path. This works but generates
   extra files and relies on meta-refresh redirects (not ideal for SEO).

3. **Accept new URLs**: Use directory-style `/posts/slug/` everywhere and let old
   `.html` URLs 404. Simplest but breaks existing links.

**Recommendation**: Use option 1 (nginx rewrite) for the server, plus option 2
(aliases) as a belt-and-suspenders fallback for crawlers that don't follow
server-side redirects.

Example nginx rewrite:

```nginx
# Redirect old .html URLs to directory-style
location ~ ^(/posts/.+)\.html$ {
    return 301 $1/;
}
location ~ ^/(about|archive|found|nfc)\.html$ {
    return 301 /$1/;
}
```

## Markup Faithfulness

### What stays identical

- **Markdown content**: Post bodies are byte-identical between Hakyll and Zola
  source files. Only the front matter format changes (YAML `---` → TOML `+++`).
- **Inline HTML**: `<figure>`, `<img>`, `<blockquote>`, `<figcaption>` etc. pass
  through both Pandoc (Hakyll) and Zola's markdown renderer unchanged.
- **Image paths**: Both serve static files from `/static/` at the root URL.
- **CSS/SCSS**: Zola compiles SASS natively; the same `default.scss` works.
- **Template structure**: Same HTML skeleton (header, nav, content, footer).

### Known differences

| Area                 | Hakyll (Pandoc)                   | Zola (pulldown-cmark + tera)                | Impact                                                                                                                                                                                                                                           |
| -------------------- | --------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Markdown engine      | Pandoc                            | pulldown-cmark                              | Minor rendering differences possible in edge cases (footnotes, definition lists, some HTML entity handling)                                                                                                                                      |
| Code highlighting    | Pandoc + skylighting              | Zola built-in (syntect)                     | Different `<span>` class names in code blocks. Existing `syntax.css` targets Pandoc classes (`code span.kw` etc.). Need to either: (a) use Zola's built-in theme that matches, or (b) generate a custom syntect CSS matching the Solarized theme |
| HTML entity escaping | Pandoc's escaping                 | Tera's `&#x2F;` for `/` in titles           | Minor: `/` in page titles becomes `&#x2F;` in HTML attributes                                                                                                                                                                                    |
| Feed format          | Hakyll's `renderAtom`/`renderRss` | Zola's built-in feed generation             | Structural differences in feed XML, but semantically equivalent                                                                                                                                                                                  |
| CSS path             | `/css/default.css`                | `/default.css` (Zola compiles SASS to root) | Template updated to use `/default.css`                                                                                                                                                                                                           |

### Code highlighting migration

Hakyll uses Pandoc's skylighting which generates classes like `code span.kw`,
`code span.st`, etc. Zola uses syntect which can output either:

- **Inline styles** (`highlight_code = true`, `highlight_theme = "..."` in config.toml)
- **CSS classes** (`highlight_code = true`, `extra_syntaxes_and_themes` config)

For maximum fidelity, use Zola's inline style highlighting with a Solarized
theme, which avoids the class-name mismatch entirely. The existing `syntax.css`
can be kept as a fallback.

## Front Matter Conversion

```
# Hakyll (YAML)               →  Zola (TOML)
---                               +++
title: "Post Title"               title = "Post Title"
---                               date = YYYY-MM-DD
                                   slug = "YYYY-MM-DD-slug-name"
                                   template = "post.html"
                                   +++
```

Key changes:

- **Date**: Hakyll extracts from filename (`YYYY-MM-DD-slug.md`). Zola needs
  explicit `date` in front matter.
- **Slug**: Zola defaults to filename-based slug. Set `slug` explicitly to
  preserve the `YYYY-MM-DD-slug` URL pattern.
- **Template**: Explicitly set to `post.html` (or use `page_template` in section
  `_index.md` to apply to all posts).

## Full Migration Checklist

- [ ] Port all 45 posts (mechanical: change front matter, add date/slug)
- [ ] Port remaining standalone pages (found.md, nfc.md)
- [ ] Code highlighting: verify Solarized theme renders correctly
- [ ] MathJax: verify LaTeX in posts renders (MathJax loaded in base template)
- [ ] Feeds: verify atom.xml and rss.xml content/structure
- [ ] nginx config: add `.html` → directory URL rewrites
- [ ] Cluster deployment: nginx + static content (ConfigMap or baked image)
- [ ] DNS/routing: update HTTPRoute if hostname changes
- [ ] Remove old `website/` directory and rules_haskell config from MODULE.bazel
- [ ] Update README.md directory index
