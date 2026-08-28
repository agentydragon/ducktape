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
By default, `index.html` references the entry by its stable name (`/main.js`).
Apps that opt into `fingerprint = True` instead emit content-hashed assets under
`/assets/` and get an `index.html` rewritten from explicit placeholders.
"""

load("@aspect_bazel_lib//lib:copy_to_directory.bzl", "copy_to_directory")
load("@aspect_rules_esbuild//esbuild:defs.bzl", "esbuild")

def _fingerprinted_spa_bundle_impl(ctx):
    esbuild_outputs = ctx.attr.esbuild[DefaultInfo].files.to_list()
    esbuild_dirs = [f for f in esbuild_outputs if f.is_directory]
    metafiles = [f for f in esbuild_outputs if f.basename.endswith("_metadata.json")]
    if len(esbuild_dirs) != 1:
        fail("fingerprinted spa bundle expected exactly one esbuild output directory, got %d" % len(esbuild_dirs))
    if len(metafiles) != 1:
        fail("fingerprinted spa bundle expected exactly one esbuild metafile, got %d" % len(metafiles))

    asset_manifest_entries = []
    asset_files = []
    for asset_target, placeholder in ctx.attr.fingerprinted_assets.items():
        files = asset_target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("fingerprinted asset %s must produce exactly one file, got %d" % (asset_target.label, len(files)))
        asset = files[0]
        asset_files.append(asset)
        asset_manifest_entries.append({
            "basename": asset.basename,
            "path": asset.path,
            "placeholder": placeholder,
        })

    asset_manifest = ctx.actions.declare_file(ctx.label.name + "_fingerprinted_assets.json")
    ctx.actions.write(asset_manifest, json.encode(asset_manifest_entries))

    out_dir = ctx.actions.declare_directory(ctx.attr.out_dir)
    args = ctx.actions.args()
    args.add("--esbuild-dir", esbuild_dirs[0].path)
    args.add("--metafile", metafiles[0].path)
    args.add("--index-template", ctx.file.index_html.path)
    args.add("--asset-manifest", asset_manifest.path)
    args.add("--out-dir", out_dir.path)
    args.add("--entry-placeholder", ctx.attr.entry_placeholder)
    args.add("--url-prefix", ctx.attr.url_prefix)

    ctx.actions.run(
        executable = ctx.executable._tool,
        arguments = [args],
        inputs = depset(
            [esbuild_dirs[0], metafiles[0], ctx.file.index_html, asset_manifest] + asset_files,
        ),
        outputs = [out_dir],
        mnemonic = "FingerprintSpaBundle",
        progress_message = "Assembling fingerprinted SPA bundle %s" % ctx.label,
    )
    return [DefaultInfo(files = depset([out_dir]))]

_fingerprinted_spa_bundle = rule(
    implementation = _fingerprinted_spa_bundle_impl,
    attrs = {
        "esbuild": attr.label(mandatory = True),
        "index_html": attr.label(mandatory = True, allow_single_file = True),
        "fingerprinted_assets": attr.label_keyed_string_dict(
            allow_files = True,
            doc = "Extra files to copy under /assets/ with content-hashed names: label -> index.html placeholder.",
        ),
        "entry_placeholder": attr.string(default = "__SPA_ENTRY__"),
        "out_dir": attr.string(default = "dist"),
        "url_prefix": attr.string(default = "/"),
        "_tool": attr.label(
            default = "//devinfra/js:spa_fingerprint_bin",
            executable = True,
            cfg = "exec",
        ),
    },
)

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
        fingerprint = False,
        fingerprinted_assets = None,
        entry_placeholder = "__SPA_ENTRY__",
        url_prefix = "/",
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
        fingerprint: If True, emit entry/chunk/assets under `assets/[name]-[hash]`
            and rewrite `index_html` placeholders to the generated URLs.
        fingerprinted_assets: Extra single-file labels to copy under `/assets/`
            with content-hashed names, as `{label: placeholder}`. Use this for
            assets produced outside esbuild, such as a Tailwind CLI stylesheet.
        entry_placeholder: Placeholder in `index_html` for the esbuild entry URL.
        url_prefix: URL prefix prepended to generated asset paths.
        visibility: Visibility of the produced directory target.
        **kwargs: Passed through to the underlying `esbuild` rule.
    """
    if config != None and (jsx != None or loader != None):
        fail("spa_bundle: pass jsx/loader OR config (a plugin esbuild.config.mjs), not both")
    if fingerprint and config != None:
        fail("spa_bundle: fingerprint=True currently supports inline jsx/loader config only")
    if fingerprint and index_html == None:
        fail("spa_bundle: fingerprint=True requires an index_html template")
    if fingerprint and public:
        fail("spa_bundle: fingerprint=True does not yet support public=; pass files through fingerprinted_assets")

    esbuild_config = config
    if config == None:
        esbuild_config = {}
        if jsx != None:
            esbuild_config["jsx"] = jsx
        if loader != None:
            esbuild_config["loader"] = loader
        if fingerprint:
            esbuild_config.update({
                "assetNames": "assets/[name]-[hash]",
                "chunkNames": "assets/[name]-[hash]",
                "entryNames": "assets/[name]-[hash]",
            })

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
        metafile = fingerprint,
        minify = minify,
        splitting = splitting,
        **kwargs
    )

    if fingerprint:
        _fingerprinted_spa_bundle(
            name = name,
            esbuild = ":" + esbuild_name,
            index_html = index_html,
            fingerprinted_assets = fingerprinted_assets or {},
            entry_placeholder = entry_placeholder,
            out_dir = out_dir,
            url_prefix = url_prefix,
            visibility = visibility,
        )
        return

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
