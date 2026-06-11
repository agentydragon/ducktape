use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use serde::Serialize;
use swc_ecma_ast::Id;

use artifact::ChunkBundle;
use spec::{PartialSwapKind, VendorRole, WrapperShape};

pub(crate) trait PartialSwapResolutionSymbols {
    fn symbols_mut(&mut self) -> &mut BTreeMap<String, PartialSwapSymbolResolution>;
}

#[derive(Debug, Clone)]
pub struct VendorAnnotationsManifest {
    pub counts: VendorAnnotationCounts,
    pub annotations: Vec<VendorAnnotationSummary>,
}

#[derive(Debug, Clone)]
pub struct VendorAnnotationCounts {
    pub annotations: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct VendorAnnotationSummary {
    pub chunk_path: String,
    pub chunk_id: String,
    pub identity: String,
    pub level: VendorAnnotationLevel,
    pub role: VendorRole,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub package: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub subpath: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VendorAnnotationLevel {
    Suppress,
    BoundaryRename,
    Swap,
    PartialSwap,
    BundledPartialSwap,
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

#[derive(Debug, Clone)]
pub struct SwapVendorOptions<'a> {
    pub package_roots: &'a std::collections::HashMap<String, PathBuf>,
    pub packages_root: &'a Option<PathBuf>,
    pub output_manifest_path: Option<PathBuf>,
    pub output_wrapper_dir: Option<PathBuf>,
    pub write: bool,
}

pub struct ApplyPartialVendorSwapsResult {
    pub artifact: ChunkBundle,
    pub manifest: PartialSwapResolutionManifest,
}

#[derive(Debug, Clone)]
pub struct PartialSwapResolutionManifest {
    pub resolutions: BTreeMap<String, ChunkPartialSwapResolution>,
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

#[derive(Debug, Clone)]
pub struct ApplyPartialVendorSwapsOptions<'a> {
    pub package_roots: &'a std::collections::HashMap<String, PathBuf>,
    pub packages_root: &'a Option<PathBuf>,
}

pub struct ApplyBundledPartialVendorSwapsResult {
    pub artifact: ChunkBundle,
    pub manifest: BundledPartialSwapResolutionManifest,
    pub self_rewrite_import_locals_by_chunk_path: BTreeMap<String, BTreeSet<Id>>,
}

#[derive(Debug, Clone)]
pub struct BundledPartialSwapResolutionManifest {
    pub resolutions: BTreeMap<String, ChunkBundledPartialSwapResolution>,
    pub counts: PartialSwapResolutionCounts,
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

#[derive(Debug, Clone)]
pub struct ApplyBundledPartialVendorSwapsOptions<'a> {
    pub package_roots: &'a std::collections::HashMap<String, PathBuf>,
    pub packages_root: &'a Option<PathBuf>,
    pub output_manifest_path: Option<PathBuf>,
    pub output_wrapper_dir: Option<PathBuf>,
    pub write: bool,
}
