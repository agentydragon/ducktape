//! Typed deserialisation surface for `js.ast_transform_spec` JSONC files.
//!
//! The spec carries three declarative top-level maps consumed by pipeline
//! stages:
//!
//! - `vendor` keyed by chunk path (`"static/lib.js"` → [`VendorMark`]).
//! - `logical_modules` keyed by chunk id, then target path
//!   (`"static/app"` → `"foo/bar/baz.js"` → [`LogicalModule`]).
//! - `residual_modules` keyed by chunk id (`"static/app"` →
//!   [`ResidualModule`]). At most one residual per chunk — encoded by the
//!   map shape.
//!
//! All consumers see typed structs; nothing here returns
//! `serde_json::Value` for a known field.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TransformSpec {
    pub kind: String,
    pub inputs: LoadJsChunksArgs,
    pub pipeline: Vec<TransformStage>,
    #[serde(default)]
    pub vendor: BTreeMap<String, VendorMark>,
    #[serde(default)]
    pub logical_modules: BTreeMap<String, BTreeMap<String, LogicalModule>>,
    #[serde(default)]
    pub residual_modules: BTreeMap<String, ResidualModule>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TransformStage {
    pub id: String,
    pub operation: String,
    #[serde(default)]
    pub args: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LoadJsChunksArgs {
    pub input_root: PathBuf,
    pub js_list_path: PathBuf,
}

// --- Vendor ---------------------------------------------------------------

/// One vendor annotation, keyed in the spec by chunk path
/// (e.g. `"static/lib.js"`). The `level` discriminator selects between
/// `suppress` / `boundary-rename` / `swap`; only `swap` requires the
/// `package`/`version`/`subpath` triple, encoded as the
/// [`VendorLevel::Swap`] variant carrying those fields.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct VendorMark {
    pub id: String,
    pub identity: String,
    pub evidence: Vec<Evidence>,
    #[serde(default)]
    pub role: VendorRole,
    #[serde(default)]
    pub upstream_family: Option<String>,
    #[serde(default)]
    pub confidence: Option<String>,
    #[serde(default)]
    pub notes: Option<String>,
    #[serde(default)]
    pub export_shape: Option<serde_json::Map<String, serde_json::Value>>,
    #[serde(default)]
    pub fingerprint: Option<Fingerprint>,
    #[serde(flatten)]
    pub level: VendorLevel,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "level", rename_all = "kebab-case")]
pub enum VendorLevel {
    Suppress,
    BoundaryRename,
    Swap(SwapMark),
}

impl VendorLevel {
    pub fn as_str(&self) -> &'static str {
        match self {
            VendorLevel::Suppress => "suppress",
            VendorLevel::BoundaryRename => "boundary-rename",
            VendorLevel::Swap(_) => "swap",
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SwapMark {
    pub package: String,
    pub version: String,
    pub subpath: String,
    #[serde(default)]
    pub wrapper_shape: Option<WrapperShape>,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Default, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum VendorRole {
    #[default]
    Module,
    Worker,
}

impl VendorRole {
    pub fn as_str(&self) -> &'static str {
        match self {
            VendorRole::Module => "module",
            VendorRole::Worker => "worker",
        }
    }
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "kebab-case")]
pub enum WrapperShape {
    NamedFromDefault,
    NamedFromJsonDefault,
    NamedFromModuleDefault,
}

impl WrapperShape {
    pub fn as_str(&self) -> &'static str {
        match self {
            WrapperShape::NamedFromDefault => "named-from-default",
            WrapperShape::NamedFromJsonDefault => "named-from-json-default",
            WrapperShape::NamedFromModuleDefault => "named-from-module-default",
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Evidence {
    pub path: String,
    pub line: u64,
    pub text: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Fingerprint {
    pub algorithm: String,
    pub hash: String,
}

// --- Logical / Residual modules ------------------------------------------

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LogicalModule {
    /// Optional human-readable id used in cycle reports and the per-chunk
    /// requested-modules summary. Defaults to a stable derived value when
    /// absent.
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub members: Vec<Member>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ResidualModule {
    #[serde(default)]
    pub id: Option<String>,
    /// Logical-module path the residual catch-all writes to. Defaults to
    /// `"residual/unhandled"` when absent.
    #[serde(default)]
    pub target: Option<String>,
    #[serde(default)]
    pub members: Vec<Member>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Member {
    /// Public export name. Defaults to the bound `selector.binding.name`.
    #[serde(default)]
    pub name: Option<String>,
    pub selector: MemberSelector,
    #[serde(default)]
    pub purity: MemberPurity,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemberSelector {
    pub binding: BindingSelector,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BindingSelector {
    pub name: String,
    #[serde(default)]
    pub kind: Option<BindingSourceKind>,
}

#[derive(Debug, Clone, Copy, Deserialize, Eq, PartialEq)]
pub enum BindingSourceKind {
    /// The bound name comes from an `import` specifier in the source
    /// chunk, not a top-level decl. The materializer rewrites the import
    /// statement to a re-import in the destination module.
    ImportSpecifier,
    /// Top-level `var` / `let` / `const` declaration in the source chunk.
    /// Carried for documentation; no special materializer path.
    VariableDeclarator,
    /// Top-level `function` declaration in the source chunk.
    FunctionDeclaration,
    /// Top-level `class` declaration in the source chunk.
    ClassDeclaration,
}

#[derive(Debug, Clone, Copy, Deserialize, Default, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum MemberPurity {
    #[default]
    Default,
    /// Author asserts that calls to the bound function have no observable
    /// side effects. Validator drops `S` edges for `<binding>(...)` call
    /// sites. See AGENTS.md "Declared purity" + DESIGN.md A9.
    Pure,
}
