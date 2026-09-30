// Keep these helpers as separate modules; expose their internal API to the
// lowering crate root without adding them to its public API.
pub(super) mod cross_chunk_imports;
pub(super) mod import_emit;
pub(super) mod imports_cross;
pub(super) mod imports_runtime;
pub(super) mod plan_references;
pub(super) mod runtime_imports;
pub(super) mod vendor_imports;

pub use cross_chunk_imports::naturalize_cross_chunk_imports;

pub(super) use imports_cross::{
    ImportLocalRenameSink, collect_entry_exports_by_original_local, cross_module_imports_for_plan,
    final_module_exports, phantom_side_effect_imports, residual_entry_imports_for_moved_body,
};
pub(super) use imports_runtime::{
    group_specifiers_into_import_decls, import_decl_module_item, resolve_imported_binding,
    source_chunk_imports_for_moved_body,
};
pub(super) use plan_references::{
    ArtifactSourceImportResolutionCache, EntryExport, ModuleReferenceNeeds, RuntimeImportLookup,
    collect_imported_reexports_by_module, plan_module_reference_needs,
};
pub(super) use runtime_imports::{
    RuntimeImportFacts, RuntimeImportInfo, RuntimeImportKind, imported_binding_named_specifier,
    record_runtime_imports, runtime_reimport_named_specifier, runtime_reimport_specifier,
};
pub(super) use vendor_imports::{
    PlannedVendorReimports, VendorReimportOracle, plan_vendor_reimports,
};
