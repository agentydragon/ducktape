use crate::PartialSwapKind;
use crate::WrapperShape;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;

/// One vendor annotation, keyed in the spec by chunk path
/// (e.g. `"static/lib.js"`). The `level` discriminator selects between
/// `swap` / `partial_swap` / `bundled_partial_swap`; `swap` carries the
/// `package`/`version`/`subpath` triple, encoded as the
/// [`VendorLevel::Swap`] variant carrying those fields.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct VendorMark {
    pub identity: String,
    #[serde(flatten)]
    pub level: VendorLevel,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "level", rename_all = "snake_case")]
pub enum VendorLevel {
    Swap(SwapMark),
    PartialSwap(PartialSwapMark),
    BundledPartialSwap(BundledPartialSwapMark),
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct SwapMark {
    pub package: String,
    pub version: String,
    pub subpath: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub wrapper_shape: Option<WrapperShape>,
    /// Upstream named exports the author asserts are aliases of the
    /// swapped package's default export. The `named_from_module_default`
    /// soundness check normally verifies, from the upstream chunk alone,
    /// that each named export shares a local binding with the chunk's own
    /// `default` export. A chunk that re-exports the package default under
    /// a single minified name without also exporting it as `default`
    /// (`export { Ft as c }`) gives the static check nothing to anchor on,
    /// so it cannot prove the alias even when it holds. Listing the export
    /// name here records the author's verified assertion (e.g. confirmed by
    /// the binding's runtime identity/version) and admits it as a default
    /// alias. Only consulted for `wrapper_shape: named_from_module_default`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub default_export_aliases: Vec<String>,
}

/// Per-symbol vendor swap on a mixed chunk. The chunk stays on disk
/// (residual exports keep working). Each listed export is rewritten
/// according to its [`PartialSwapKind`] — see the variants for the
/// three supported shapes (`member` for per-symbol member-access
/// rewrites à la zod; `namespace` and `default` for libraries the
/// chunk re-exports as a whole namespace or as a default value).
/// Multiple upstream packages can share one chunk.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PartialSwapMark {
    /// package_name → upstream coordinates.
    pub packages: BTreeMap<String, PartialSwapPackage>,
    /// chunk_export_name → which package + how to rewrite it.
    pub symbols: BTreeMap<String, PartialSwapSymbol>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PartialSwapPackage {
    pub version: String,
    pub subpath: String,
    /// Local identifier used in the emitted
    /// `import * as <namespace> from "<package>"` for symbols with
    /// `kind: member`. Ignored for `kind: namespace` and
    /// `kind: default`, which use the caller-side local binding name
    /// for the import alias instead.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PartialSwapSymbol {
    /// Key into the enclosing [`PartialSwapMark::packages`] map.
    pub package: String,
    /// How to rewrite references to the caller's local binding. See
    /// [`PartialSwapKind`].
    #[serde(default, skip_serializing_if = "is_default_partial_swap_kind")]
    pub kind: PartialSwapKind,
    /// `kind: member` and `kind: named` only: upstream package's
    /// export name. For `member` it becomes the member accessed off
    /// the namespace import; for `named` it becomes the imported name
    /// in `import { <upstream_export> as <local_binding> } from "<pkg>"`.
    /// Forbidden for `kind: namespace` / `kind: default`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub upstream_export: Option<String>,
    /// Optional chunk-local binding that implements this swapped symbol.
    ///
    /// Normally the debundler discovers the implementation binding from
    /// the chunk's `export { local as <symbol-key> }` specifier. Some
    /// bundled vendors also have reachable internal helpers that are not
    /// exported by the chunk but still need to be replaced when residual
    /// in-chunk code calls them. In that case, set `local` to the
    /// top-level binding name to rewrite/strip.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local: Option<String>,
}

/// Per-symbol vendor swap backed by a caller-supplied ESM bundle.
/// The input package may be CJS or otherwise browser-hostile; the
/// debundler does not run a bundler itself. Instead, `bundle.path`
/// names a prebuilt ESM blob whose named exports are projected into
/// per-package facades under `vendors/generated`, and consumer imports
/// are rewritten to those facades.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BundledPartialSwapMark {
    pub bundle: BundledPartialSwapBundle,
    /// package_name -> upstream metadata and bundle projection.
    pub packages: BTreeMap<String, BundledPartialSwapPackage>,
    /// chunk_export_name -> which package + how to rewrite it.
    pub symbols: BTreeMap<String, PartialSwapSymbol>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BundledPartialSwapBundle {
    pub path: PathBuf,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BundledPartialSwapPackage {
    pub version: String,
    pub subpath: String,
    /// Named export in [`BundledPartialSwapMark::bundle`] that is the
    /// package object/default value for this package coordinate.
    pub bundle_export: String,
    /// Local identifier used for member/named rewrites that need a
    /// stable imported object. Ignored for `kind: namespace` and
    /// `kind: default`, which use the caller-side local binding.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
}

fn is_default_partial_swap_kind(kind: &PartialSwapKind) -> bool {
    matches!(kind, PartialSwapKind::Member)
}
