"""Shared macro for bundling a single-page app with esbuild.

`spa_bundle` is the one bundling pattern for browser SPAs in this repo. It wraps
the native `esbuild()` rule from `rules_esbuild` (not a hand-rolled
`esbuild.config.mjs` driver, and not Vite) plus the static-asset plumbing every
app repeats: copy `index.html` and a `public/` tree next to the emitted JS/CSS
into a single served `dist/` directory.

Why the native `esbuild()` rule: it ships a bazel-sandbox module resolver that
understands `rules_js`'s symlinked `node_modules` layout. Vite/Rollup follow
those symlinks out of the sandbox (the "artifact prefix conflict" that blocked
`x/rspcache/admin_ui`), so esbuild is the hermetic choice under Bazel.

Two configuration paths:

  - **Plugin-free (React)**: pass `jsx`/`loader` and the macro builds the inline
    esbuild `config` dict. No `esbuild.config.mjs` needed.
  - **Plugins (Svelte, Tailwind, svg, …)**: pass `config` = a `js_library`
    wrapping an `esbuild.config.mjs` that `export default {plugins: [...]}` with
    the plugin npm packages as that library's `deps`. The native rule threads
    plugins in through this label (see rules_esbuild `examples/plugins`). When
    `config` is set, the app's mjs owns jsx/loader/conditions/aliases.

The bundle is emitted into an `output_dir` TreeArtifact so CSS imports and code
splitting (multiple chunks) are captured without enumerating output filenames.
`index.html` references the entry by its stable name (`/main.js`), so no
content-hash rewrite is needed.
"""

load("@aspect_bazel_lib//lib:copy_to_directory.bzl", "copy_to_directory")
load("@aspect_rules_esbuild//esbuild:defs.bzl", "esbuild")

def spa_bundle(
        name,
        entry_point,
        deps,
        index_html = "index.html",
        config = None,
        jsx = None,
        loader = None,
        splitting = False,
        public = None,
        public_dir = "public",
        out_dir = "dist",
        target = "es2022",
        minify = True,
        sourcemap = "linked",
        visibility = None,
        **kwargs):
    """Bundle a browser SPA into a served directory of static assets.

    Args:
        name: Target name. The result is a directory (`out_dir`) suitable for a
            FastAPI `StaticFiles(directory=...)` mount or an nginx `oci_image`.
        entry_point: The entry source file (e.g. `main.tsx`, `src/main.ts`).
        deps: `js_library` targets forming the app's module graph (incl. the
            entry's library and its `//:node_modules/*` deps).
        index_html: HTML shell copied to `<out_dir>/index.html`. Must reference
            the bundle as `/main.js` (and `/main.css` if CSS is imported). Pass
            `None` when the app's index.html is served separately (e.g. a backend
            packages it next to, not inside, the bundle dir).
        config: Optional label of a `js_library` exporting esbuild options for
            plugin-based bundling (Svelte/Tailwind). Mutually exclusive with
            `jsx`/`loader`, which the mjs sets instead.
        jsx: esbuild `jsx` mode, e.g. `"automatic"` for React. Inline-config path.
        loader: esbuild `loader` dict, e.g. `{".js": "jsx"}`. Inline-config path.
        splitting: Enable esbuild code splitting (emits shared chunks).
        public: Files copied verbatim into `out_dir`, with `public_dir` stripped
            (e.g. `glob(["public/**/*"])` → `public/icon.svg` lands at `/icon.svg`).
        public_dir: Package-relative directory prefix stripped from `public`.
        out_dir: Name of the produced served directory (default `dist`).
        target: esbuild target (default `es2022`).
        minify: Minify the output (default True).
        sourcemap: esbuild sourcemap mode (default `linked`).
        visibility: Visibility of the produced directory target.
        **kwargs: Passed through to the underlying `esbuild` rule.
    """
    if config != None and (jsx != None or loader != None):
        fail("spa_bundle: pass jsx/loader OR config (a plugin esbuild.config.mjs), not both")

    esbuild_config = config
    if config == None:
        esbuild_config = {}
        if jsx != None:
            esbuild_config["jsx"] = jsx
        if loader != None:
            esbuild_config["loader"] = loader

    esbuild_name = name + "_esbuild"
    esbuild(
        name = esbuild_name,
        entry_point = entry_point,
        deps = deps,
        config = esbuild_config,
        output_dir = True,
        format = "esm",
        platform = "browser",
        target = [target],
        sourcemap = sourcemap,
        minify = minify,
        splitting = splitting,
        **kwargs
    )

    pkg = native.package_name()
    esbuild_root = "{}/{}".format(pkg, esbuild_name) if pkg else esbuild_name

    # Strip prefixes so the esbuild outputs, index.html, and public/ assets all
    # land at the root of out_dir. Longest-matching root first; the bare package
    # path (catching index.html) must come last.
    root_paths = [esbuild_root]
    if public:
        root_paths.append("{}/{}".format(pkg, public_dir) if pkg else public_dir)
    root_paths.append(pkg if pkg else ".")

    srcs = [":" + esbuild_name] + ([index_html] if index_html else []) + (public or [])
    copy_to_directory(
        name = name,
        srcs = srcs,
        out = out_dir,
        root_paths = root_paths,
        visibility = visibility,
    )
