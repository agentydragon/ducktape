use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;

use artifact::ChunkBundle;
use spec::{PartialSwapKind, WrapperShape};

#[derive(Debug, Clone)]
pub struct VendorResolutionManifest {
    pub resolutions: BTreeMap<String, VendorResolution>,
    pub counts: VendorResolutionCounts,
}

pub struct SwapVendorChunksResult {
    pub artifact: ChunkBundle,
    pub manifest: VendorResolutionManifest,
    pub removed_chunk_ids: BTreeSet<String>,
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

#[derive(Debug, Clone)]
pub struct VendorResolutionCounts {
    pub swapped: usize,
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
