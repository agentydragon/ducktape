//! Runtime destinations are distinct from semantic acceptance of a partition.

use serde::Serialize;
use std::collections::BTreeMap;

use crate::paths::{
    join_module_path, module_path_dirname, normalize_module_path, relative_module_path,
};

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeResourceDestination {
    Captured { path: String },
    Emitted { path: String },
    Replacement { package: String },
    External { url: String },
    Unresolved { reason: UnresolvedResourceReason },
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum UnresolvedResourceReason {
    NonLiteral,
    UnverifiedPath { path: String },
    InvalidLocalPath,
    UnresolvedUrlIdentity,
    DocumentRelativeWorker,
    MissingCapture { path: String },
    MissingEmission { path: String },
}

impl RuntimeResourceDestination {
    /// Render a represented destination relative to an emitted caller. Unknown
    /// nonliteral resources retain the original spelling. An unverified local
    /// path can still be rebased without claiming that its file exists.
    pub fn relative_url(&self, caller_file: &str) -> Option<String> {
        let path = match self {
            Self::Captured { path } | Self::Emitted { path } => path,
            Self::Unresolved {
                reason: UnresolvedResourceReason::UnverifiedPath { path },
            } => path,
            Self::Replacement { .. } => return None,
            Self::External { url } => return Some(url.clone()),
            Self::Unresolved { .. } => return None,
        };
        let relative = relative_module_path(&module_path_dirname(caller_file), path);
        Some(if relative.starts_with('.') {
            relative
        } else {
            format!("./{relative}")
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeResourceKind {
    DynamicImport,
    Worker,
    Url,
}

#[derive(Debug, Clone, Serialize)]
pub struct RuntimeResourceUse {
    pub kind: RuntimeResourceKind,
    pub specifier: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RuntimeResourceResolution {
    pub source: String,
    pub resource: RuntimeResourceUse,
    pub destination: RuntimeResourceDestination,
}

pub type RuntimeResourceDestinations =
    BTreeMap<(RuntimeResourceKind, String), RuntimeResourceDestination>;

/// Resolve local runtime coordinates without claiming the file was captured.
/// URL schemes, network-relative URLs and bare module specifiers stay external.
pub fn local_resource_path(
    source_file: &str,
    specifier: &str,
    kind: RuntimeResourceKind,
) -> Option<String> {
    if specifier.starts_with("//")
        || url::Url::parse(specifier).is_ok()
        || (kind == RuntimeResourceKind::DynamicImport
            && !specifier.starts_with('.')
            && !specifier.starts_with('/'))
    {
        return None;
    }
    let path = if let Some(root_relative) = specifier.strip_prefix('/') {
        root_relative.to_string()
    } else {
        join_module_path(&[&module_path_dirname(source_file), specifier])
    };
    normalize_module_path(&path).ok()
}
