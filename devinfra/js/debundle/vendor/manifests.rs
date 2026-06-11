use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;
use swc_ecma_ast::Id;

use artifact::ChunkBundle;
use spec::{PartialSwapKind, WrapperShape};

pub(crate) trait PartialSwapResolutionSymbols {
    fn symbols_mut(&mut self) -> &mut BTreeMap<String, PartialSwapSymbolResolution>;
}

#[derive(Debug, Clone)]
pub struct RenameVendorExportsManifest {
    pub counts: RenameVendorExportsCounts,
    pub details: Vec<RenameVendorExportsDetail>,
}

pub struct RenameVendorExportsResult {
    pub artifact: ChunkBundle,
    pub manifest: RenameVendorExportsManifest,
}

#[derive(Debug, Clone)]
pub struct RenameVendorExportsCounts {
    pub considered: usize,
    pub chunks_with_mapping: usize,
    pub rewrites: usize,
}

#[derive(Debug, Clone)]
pub struct RenameVendorExportsDetail {
    pub chunk_path: String,
    pub chunk_id: String,
    pub mapping_size: usize,
    pub rewrites: usize,
    pub callers: Vec<RenameVendorExportsCaller>,
}

#[derive(Debug, Clone)]
pub struct RenameVendorExportsCaller {
    pub file: String,
    pub rewrites: usize,
}

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

pub struct ApplyPartialVendorSwapsResult {
    pub artifact: ChunkBundle,
    pub manifest: ResolutionManifest<ChunkPartialSwapResolution>,
}

/// Wire manifest of a partial-swap-family wave: per-chunk resolutions
/// (projections of the vendor plan) keyed by chunk path, plus totals
/// accumulated at application time.
#[derive(Debug, Clone)]
pub struct ResolutionManifest<R> {
    pub resolutions: BTreeMap<String, R>,
    pub counts: PartialSwapResolutionCounts,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkPartialSwapResolution {
    pub chunk_id: String,
    pub chunk_path: String,
    pub packages: BTreeMap<String, PartialSwapPackageResolution>,
    pub symbols: BTreeMap<String, PartialSwapSymbolResolution>,
}

impl PartialSwapResolutionSymbols for ChunkPartialSwapResolution {
    fn symbols_mut(&mut self) -> &mut BTreeMap<String, PartialSwapSymbolResolution> {
        &mut self.symbols
    }
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

#[derive(Debug, Clone)]
pub struct PartialSwapResolutionCounts {
    pub chunks: usize,
    pub symbols: usize,
    pub references_rewritten: usize,
}

pub struct ApplyBundledPartialVendorSwapsResult {
    pub artifact: ChunkBundle,
    pub manifest: ResolutionManifest<ChunkBundledPartialSwapResolution>,
    pub self_rewrite_import_locals_by_chunk_path: BTreeMap<String, BTreeSet<Id>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkBundledPartialSwapResolution {
    pub chunk_id: String,
    pub chunk_path: String,
    pub bundle: BundledPartialSwapBundleResolution,
    pub packages: BTreeMap<String, BundledPartialSwapPackageResolution>,
    pub symbols: BTreeMap<String, PartialSwapSymbolResolution>,
}

impl PartialSwapResolutionSymbols for ChunkBundledPartialSwapResolution {
    fn symbols_mut(&mut self) -> &mut BTreeMap<String, PartialSwapSymbolResolution> {
        &mut self.symbols
    }
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
