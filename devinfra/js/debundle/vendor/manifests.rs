use std::collections::BTreeMap;

use serde::Serialize;

use spec::{PartialSwapKind, WrapperShape};

/// Combined manifest of the replacements in one reconstructed program.
#[derive(Debug, Default, Serialize)]
pub struct VendorSwapsReport {
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub full: BTreeMap<String, VendorResolution>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub partial: BTreeMap<String, ChunkPartialSwapResolution>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub bundled_partial: BTreeMap<String, ChunkBundledPartialSwapResolution>,
}

impl VendorSwapsReport {
    pub fn is_empty(&self) -> bool {
        self.full.is_empty() && self.partial.is_empty() && self.bundled_partial.is_empty()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct VendorResolution {
    pub chunk_id: String,
    pub chunk_path: String,
    pub entry_file: String,
    pub package: String,
    pub version: String,
    pub subpath: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub wrapper_shape: Option<WrapperShape>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub generated_wrapper_path: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkPartialSwapResolution {
    pub chunk_id: String,
    pub chunk_path: String,
    pub packages: BTreeMap<String, PartialSwapPackageResolution>,
    pub symbols: BTreeMap<String, PartialSwapSymbolResolution>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PartialSwapPackageResolution {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
    pub version: String,
    pub subpath: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct PartialSwapSymbolResolution {
    pub package: String,
    pub kind: PartialSwapKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub upstream_export: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub local: Option<String>,
    pub references_rewritten: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkBundledPartialSwapResolution {
    pub chunk_id: String,
    pub chunk_path: String,
    pub bundle: BundledPartialSwapBundleResolution,
    pub packages: BTreeMap<String, BundledPartialSwapPackageResolution>,
    pub symbols: BTreeMap<String, PartialSwapSymbolResolution>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BundledPartialSwapBundleResolution {
    pub source_path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub generated_bundle_path: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BundledPartialSwapPackageResolution {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
    pub version: String,
    pub subpath: String,
    pub bundle_export: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub generated_facade_path: Option<String>,
}
