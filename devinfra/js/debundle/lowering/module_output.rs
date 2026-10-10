//! Emit one accepted logical-module plan. Chunk selection and entry construction stay in
//! `lower`; this phase owns per-module imports, sealed renames, comments, and output metadata.

use std::sync::Mutex;

use swc_common::{BytePos, DUMMY_SP, Spanned};

use super::imports::import_emit::relative_source;
use super::imports::imports_runtime::source_chunk_import_for_target;
use super::scope_names::collect_local_binding_names;

use crate::exports::{export_named_for_bindings, omit_existing_local_exports};
use crate::imports::{
    ArtifactSourceImportResolutionCache, EntryExport, ImportLocalRenameSink, ModuleReferenceNeeds,
    PlannedVendorReimports, RuntimeImportFacts, RuntimeImportLookup, VendorReimportOracle,
    cross_module_imports_for_plan, final_module_exports, group_specifiers_into_import_decls,
    imported_binding_named_specifier, phantom_side_effect_imports, plan_module_reference_needs,
    plan_vendor_reimports, residual_entry_imports_for_moved_body,
    source_chunk_imports_for_moved_body,
};
use crate::naturalize::NaturalizedModuleBody;
use crate::plans::ModulePlan;
use crate::rename_ledger::{RenameLedger, RenameScope, ScopeOccupancy, SealValidation};
use crate::rewrite_runtime::rewrite_runtime_sources_for_target;
use crate::visitors::IdentifierRenamer;
use analysis::{ModuleId, top_level_id};
use anyhow::{Result, bail};
use artifact::{
    ChunkId, FileMetadata, FileRole, JsFile, JsFileBody, SelectedModuleLowering, join_module_path,
};
use gate::ChunkFactorization;
use js_ast::{ParsedJsModule, format_comment_block_lines, set_str_value, str_value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_ecma_ast::*;
use swc_ecma_visit::VisitMutWith;

const LOWERING_FILE_PRAGMA: &str =
    "// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors";
const LOWERING_GENERATOR_HEADER: &str = "// @ducktape-generator selected_module_lowering";

struct ModuleOutputContext<'a> {
    factorization: &'a ChunkFactorization,
    runtime_ast: &'a ParsedJsModule,
    chunk_top_level_mark: swc_common::Mark,
    chunk_id: &'a str,
    entry_file: &'a str,
    source_path: &'a str,
}

pub(super) struct ModuleEmissionInputs<'a> {
    pub(super) index: usize,
    pub(super) plan: &'a ModulePlan,
    pub(super) naturalized: NaturalizedModuleBody,
    pub(super) factorization: &'a ChunkFactorization,
    pub(super) declaration_by_name: &'a HashMap<Id, Vec<usize>>,
    pub(super) binding_assignment: &'a HashMap<Id, usize>,
    pub(super) runtime_import_facts: &'a RuntimeImportFacts,
    pub(super) entry_exports_by_original_local: &'a HashMap<Id, EntryExport>,
    pub(super) imported_reexports: &'a [super::imports::plan_references::ImportedReexport],
    pub(super) selected_exports: Option<&'a BTreeMap<String, String>>,
    pub(super) cross_module_chunk_renames: &'a BTreeMap<String, String>,
    pub(super) source_import_cache: &'a Mutex<ArtifactSourceImportResolutionCache<'a>>,
    pub(super) chunk_id: &'a str,
    pub(super) chunk_id_interned: ChunkId,
    pub(super) entry_file: &'a str,
    pub(super) source_path: &'a str,
    pub(super) chunk_top_level_mark: swc_common::Mark,
    pub(super) runtime_ast: &'a ParsedJsModule,
    pub(super) vendor_import_oracle: Option<&'a VendorReimportOracle<'a>>,
}

/// One module plan's lowered output: the emitted file plus the
/// per-plan bookkeeping `lower_chunk` rolls up.
pub(super) struct LoweredModuleOutput {
    pub(super) file: JsFile,
    pub(super) record: (String, FileRole),
    pub(super) lowering: SelectedModuleLowering,
    pub(super) vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
}

pub(super) fn emit_module(inputs: ModuleEmissionInputs<'_>) -> Result<LoweredModuleOutput> {
    let ModuleEmissionInputs {
        index,
        plan,
        naturalized,
        factorization,
        declaration_by_name,
        binding_assignment,
        runtime_import_facts,
        entry_exports_by_original_local,
        imported_reexports,
        selected_exports,
        cross_module_chunk_renames,
        source_import_cache,
        chunk_id,
        chunk_id_interned,
        entry_file,
        source_path,
        chunk_top_level_mark,
        runtime_ast,
        vendor_import_oracle,
    } = inputs;
    let NaturalizedModuleBody {
        mut body,
        renames: local_renames,
        facts: body_facts,
    } = naturalized;
    let ModuleReferenceNeeds {
        cross_module_imports_by_provider,
        residual_entry_imports,
        missing_residual_exports,
        runtime_reimports,
        phantom_side_effect_providers,
    } = plan_module_reference_needs(
        index,
        &body_facts,
        factorization,
        declaration_by_name,
        binding_assignment,
        entry_exports_by_original_local,
        RuntimeImportLookup {
            imports: runtime_import_facts,
            original_by_renamed: local_renames
                .merged
                .iter()
                .map(|(pre, post)| (post.clone(), pre.clone()))
                .collect(),
        },
    );
    // Import-local mints for this plan's body flow through a per-plan
    // ledger (scope: this plan's `Module`, origin: `ImportInduced`);
    // the ledger's taken-name set is seeded with every name bound
    // anywhere in the body so mints stay capture-free, and the sealed
    // projection below is the rename map the body renamer applies.
    let module = ModuleId::logical(index);
    let module_local_names = collect_local_binding_names(&body);
    let mut import_ledger = RenameLedger::default();
    import_ledger.seed_taken(
        RenameScope::Module(module),
        module_local_names.iter().cloned(),
    );
    let mut import_rename_sink = ImportLocalRenameSink {
        module,
        chunk_top_level_mark,
        ledger: &mut import_ledger,
    };
    // All intra-chunk imports (cross-module binding imports, phantom
    // side-effect imports, and the residual-entry import) are
    // collected as `(target ModuleId, decl)` pairs and ordered by ONE
    // shared rule — `EsmImportOrder::sort_module_imports` — below.
    // The realizability gate's evaluation simulator sorts this
    // module's DFS neighbors with the same call, so the emitted
    // import order and the gate's predicted evaluation order cannot
    // diverge. See `esm_import_order` and
    // `accepted_spec_runs_under_node_test::early_entry_importer_…`.
    let mut intra_chunk_imports = cross_module_imports_for_plan(
        &plan.target_file,
        cross_module_imports_by_provider,
        factorization,
        &mut import_rename_sink,
    );
    // Phantom side-effect imports surface at-init-promotion-derived
    // constraining edges as real ESM imports so the linker's DFS
    // visits the provider modules as dependencies. See
    // `phantom_side_effect_imports`.
    intra_chunk_imports.extend(phantom_side_effect_imports(
        &plan.target_file,
        phantom_side_effect_providers,
        factorization,
    ));
    let residual_entry_import_items = residual_entry_imports_for_moved_body(
        &plan.id,
        entry_file,
        &plan.target_file,
        residual_entry_imports,
        missing_residual_exports,
        &mut import_rename_sink,
    )?;
    // Seal the per-plan import ledger; the Module-scope projection is
    // the import-local rename map the pre-ledger code accumulated
    // across the two minting passes above. Seal asserts the mints
    // stayed clear of every name bound anywhere in the body.
    let module_import_renames = import_ledger
        .seal(&SealValidation {
            occupancy: BTreeMap::from([(
                RenameScope::Module(module),
                ScopeOccupancy::Body {
                    label: plan.id.clone(),
                    root: BTreeSet::new(),
                    nested: module_local_names,
                    captured: BTreeSet::new(),
                },
            )]),
            reserved: BTreeSet::new(),
        })?
        .module_renames_by_name(module);
    if !module_import_renames.is_empty() {
        let mut renamer = IdentifierRenamer::new(&module_import_renames);
        for item in body.iter_mut() {
            item.visit_mut_with(&mut renamer);
        }
        // Import-local renames are minted from the ledger's taken set,
        // which seal validated against every name bound anywhere in the
        // body — a capture here would mean seal's guarantee broke.
        debug_assert!(
            renamer.captured.is_empty(),
            "import-local renames for module {} captured despite seal validation: {:?}",
            plan.id,
            renamer.captured,
        );
    }
    // Vendor consultation for the planned runtime re-imports: named re-imports whose source
    // directive targets a partially / bundled-partially swapped chunk
    // are constructed as package / facade imports straight from the
    // plan oracle; everything else stays a chunk re-import (with the
    // boundary-rename mapping applied at construction).
    let PlannedVendorReimports {
        retained: runtime_reimports,
        imported_overrides,
        external_imports: vendor_external_imports,
        body_rewrites: vendor_body_rewrites,
        references_rewritten: mut vendor_reference_rewrites,
    } = plan_vendor_reimports(
        runtime_reimports,
        vendor_import_oracle,
        chunk_id_interned,
        entry_file,
        &plan.target_file,
    );
    // Re-import any source-chunk import-specifier-bound locals that
    // moved code in `body` references but no top-level decl
    // satisfies (e.g. `const { decode } = gge;` where `gge` was an
    // ImportSpecifier in the source chunk's runtime body). Without
    // this, the moved code references a free variable and Node
    // throws `ReferenceError: gge is not defined` at runtime.
    //
    // `source_import_cache` is shared across parallel per-plan
    // workers; the `Mutex` serialises the BTreeMap lookups but the
    // critical section is short (a key lookup + possible insert).
    let mut runtime_reimports = {
        let mut cache = source_import_cache
            .lock()
            .expect("source_import_cache poisoned");
        // Imports peeled with their atomic unit still use source-chunk URLs.
        // Rebase those before adding imports already emitted for this target.
        for item in &mut body {
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
                let source = source_chunk_import_for_target(
                    &mut cache,
                    chunk_id,
                    entry_file,
                    &plan.target_file,
                    &str_value(&import.src),
                )?;
                set_str_value(&mut import.src, source);
            }
        }
        source_chunk_imports_for_moved_body(
            &mut cache,
            chunk_id,
            entry_file,
            &plan.target_file,
            runtime_reimports,
            &imported_overrides,
        )
    }?;
    // The residual-entry import targets the chunk's residual module
    // (the emitted entry file — always the ESM DFS root, so its slot
    // in the list is a runtime no-op, but it still gets its shared-
    // ordering position for determinism).
    let residual_module = factorization.partition.residual();
    intra_chunk_imports.extend(
        residual_entry_import_items
            .into_iter()
            .map(|item| (residual_module, item)),
    );
    // ONE ordering for every intra-chunk import — the same
    // `EsmImportOrder::sort_module_imports` the gate's simulator
    // uses for this module's DFS neighbor order. Runtime re-imports
    // (other source chunks) follow; they're outside the per-chunk
    // I-graph the gate reasons about.
    factorization
        .import_order()
        .sort_module_imports(&mut intra_chunk_imports);
    let mut combined: Vec<ModuleItem> = intra_chunk_imports
        .into_iter()
        .map(|(_, item)| item)
        .collect();
    combined.append(&mut runtime_reimports);
    combined.append(&mut body);
    body = combined;
    // Apply chunk_renames to the assembled module body so that
    // import-specifier aliases and references in the moved code
    // both pick up the spec's rename. Without this, residual
    // entry says `getMobxGlobalState` but the peeled module's
    // `import { f as cx }` and `cx()` refs still say `cx`,
    // producing two disagreeing local aliases for the same
    // upstream binding.
    if !cross_module_chunk_renames.is_empty() {
        let mut renamer = IdentifierRenamer::new(cross_module_chunk_renames);
        for item in body.iter_mut() {
            item.visit_mut_with(&mut renamer);
        }
        if !renamer.captured.is_empty() {
            // The entry-side chunk_renames validation only sees
            // entry's post-split body; a target can still collide
            // with a binding nested inside this moved module body.
            bail!(
                "chunk_renames applied to module {} would be captured by a nested binding: {:?}",
                plan.id,
                renamer.captured,
            );
        }
    }
    // External package / facade imports join the runtime re-import
    // block after the retained chunk re-imports, deterministic in
    // first-need order. External targets are outside the per-chunk
    // I-graph the gate orders. Spliced
    // after the chunk_renames application so the locals they
    // introduce are never subject to spec renames — matching the
    // post-hoc wave, which introduced them after every rename had run.
    if !vendor_external_imports.is_empty() {
        let import_count = body
            .iter()
            .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
            .count();
        let tail = body.split_off(import_count);
        body.extend(vendor_external_imports);
        body.extend(tail);
    }
    rewrite_runtime_sources_for_target(&mut body, chunk_id, entry_file, &plan.target_file);
    // Plan-driven vendor body replacements (member accesses off the
    // package / facade namespace, named-kind auto-renames). Not a
    // RenameLedger entry — ident→member-expression replacement is an
    // expression rewrite, not a rename — so it runs as a separate
    // plan-driven visitor sequenced after the sealed rename
    // application, like the runtime-URL rewrite above.
    if !vendor_body_rewrites.is_empty() {
        // `cross_module_chunk_renames` may have renamed an import
        // local's sym in place (ctxt preserved); remap the keys so
        // the replacement still matches the body's idents.
        let bindings: BTreeMap<Id, vendor::IdentRewriteTarget> = vendor_body_rewrites
            .into_iter()
            .map(
                |(id, target)| match cross_module_chunk_renames.get(id.0.as_ref()) {
                    Some(renamed) => ((renamed.as_str().into(), id.1), target),
                    None => (id, target),
                },
            )
            .collect();
        let mut rewriter = vendor::PartialSwapIdentRewriter {
            bindings: &bindings,
            references_by_symbol: &mut vendor_reference_rewrites,
        };
        for item in body.iter_mut() {
            item.visit_mut_with(&mut rewriter);
        }
    }
    // ImportSpecifier-bound members (`BindingKind::Imported` in
    // `factorization.analysis.bindings()`): for each `Imported` binding whose
    // `re_exported_by` map names this module, emit a re-import
    // (using the local name as the alias) plus mirror the
    // public-name export. Per-destination relative paths are
    // computed here so multiple modules at different output
    // depths each get a correctly-relativised path.
    let mut import_member_exports = BTreeMap::<String, String>::new();
    let reexports = imported_reexports;
    if !reexports.is_empty() {
        let import_count = body
            .iter()
            .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
            .count();
        // `imported_from` on `BindingKind::Imported` is output-tree-
        // rooted absolute; `plan.target_file` is chunk-rooted. Lift
        // the destination to the same coordinate system before
        // computing the relative path.
        let dest_abs = join_module_path(&[chunk_id, &plan.target_file]);
        // Group reexports by rewritten source so multiple bindings
        // re-exported from the same import-from end up in a single
        // `import { ... } from "<src>"` statement, not one statement
        // per binding. All specifiers emitted here are Named, so the
        // namespace-split rule in the shared grouper is a no-op for
        // this path, but routing through it keeps the grouping logic
        // single-sourced with `source_chunk_imports_for_moved_body`.
        let mut pairs: Vec<(String, ImportSpecifier)> = Vec::with_capacity(reexports.len());
        for reexport in reexports {
            let src = relative_source(&dest_abs, &reexport.imported_from);
            let specifier =
                imported_binding_named_specifier(&reexport.local, &reexport.imported_name);
            pairs.push((src, specifier));
            import_member_exports.insert(reexport.local.clone(), reexport.public_name.clone());
        }
        let reexport_imports = group_specifiers_into_import_decls(pairs);
        let tail = body.split_off(import_count);
        body.extend(reexport_imports);
        body.extend(tail);
    }
    if let Some(exports) = selected_exports {
        // Export locals remap through the explicit map only: heuristic
        // renames never rename the top-level declaration an export
        // specifier refers to, so mapping through the merged map can
        // emit `export { x as y }` for an `x` that was never declared
        // (a load-time SyntaxError).
        let mut exports = final_module_exports(exports, &local_renames.explicit);
        exports.extend(
            import_member_exports
                .iter()
                .map(|(k, v)| (k.clone(), v.clone())),
        );
        omit_existing_local_exports(&body, &mut exports);
        if !exports.is_empty() {
            body.push(export_named_for_bindings(&exports));
        }
    } else if !import_member_exports.is_empty() {
        body.push(export_named_for_bindings(&import_member_exports));
    }
    let output_context = ModuleOutputContext {
        factorization,
        runtime_ast,
        chunk_top_level_mark,
        chunk_id,
        entry_file,
        source_path,
    };
    let (file, record, lowering) =
        build_module_output(plan, body, &local_renames.explicit, &output_context);
    Ok(LoweredModuleOutput {
        file,
        record,
        lowering,
        vendor_reference_rewrites,
    })
}

fn build_module_output(
    plan: &ModulePlan,
    body: Vec<ModuleItem>,
    explicit_renames: &BTreeMap<String, String>,
    context: &ModuleOutputContext<'_>,
) -> (JsFile, (String, FileRole), SelectedModuleLowering) {
    let mut sorted_plan_bindings: Vec<(&String, &String)> = plan.bindings.iter().collect();
    sorted_plan_bindings.sort_by(|a, b| a.0.cmp(b.0));
    let binding_names: Vec<String> = sorted_plan_bindings
        .iter()
        .map(|(k, _)| (*k).clone())
        .collect();
    let exported_names: Vec<String> = sorted_plan_bindings
        .iter()
        .map(|(_, v)| (*v).clone())
        .collect();
    let spec_named_export_names: Vec<String> = sorted_plan_bindings
        .iter()
        .filter(|(binding, _)| plan.spec_named_bindings.contains(binding.as_str()))
        .map(|(_, exported)| (*exported).clone())
        .collect();
    let binding_ids: Vec<Id> = binding_names
        .iter()
        .map(|name| top_level_id(name, context.chunk_top_level_mark))
        .collect();
    let owner_ids = context
        .factorization
        .analysis
        .owner_report_ids_for_bindings(binding_ids.iter());
    // Module-level `comment:` from the spec lands at the very top of
    // the emitted file (above the lowerer's pragma block), separated
    // from the pragmas by a blank `//` line so the human-readable
    // text stays visually distinct from generator metadata. Empty /
    // absent comment emits nothing.
    let mut header: Vec<String> = Vec::new();
    if let Some(comment) = plan.comment.as_deref().filter(|c| !c.is_empty()) {
        header.extend(format_comment_block_lines(comment));
        header.push(String::new());
    }
    header.extend([
        LOWERING_FILE_PRAGMA.to_string(),
        LOWERING_GENERATOR_HEADER.to_string(),
        format!(
            "// Selected-module lowered region; original owner ids: {}.",
            owner_ids.join(", ")
        ),
        format!(
            "// Selected-module lowered region; source bindings: {}.",
            binding_names.join(", ")
        ),
    ]);
    let leading_item_comments = anonymous_statement_comments_by_span(plan, context.runtime_ast);
    // `plan.binding_comments` is keyed by the member's ORIGINAL binding
    // name, but `attach_binding_comments` matches the names the emitted
    // declarations carry AFTER naturalization renamed them — re-key
    // through the applied explicit renames or renamed members lose
    // their comments.
    let binding_comments: BTreeMap<String, String> = plan
        .binding_comments
        .iter()
        .map(|(binding, comment)| {
            (
                explicit_renames
                    .get(binding)
                    .cloned()
                    .unwrap_or_else(|| binding.clone()),
                comment.clone(),
            )
        })
        .collect();
    let file = JsFile {
        path: plan.target_file.clone(),
        body: JsFileBody::Ast(ParsedJsModule {
            cm: context.runtime_ast.cm.clone(),
            module: Module {
                span: DUMMY_SP,
                body,
                shebang: None,
            },
            unresolved_mark: context.runtime_ast.unresolved_mark,
            top_level_mark: context.runtime_ast.top_level_mark,
        }),
        header_lines: header,
        binding_comments,
        leading_item_comments,
        metadata: FileMetadata {
            chunk_id: context.chunk_id.to_string(),
            chunk_file: plan.target_file.clone(),
            role: FileRole::Module,
            source_path: context.source_path.to_string(),
        },
    };
    let record = (plan.target_file.clone(), FileRole::Module);
    let lowering = SelectedModuleLowering {
        binding_names,
        chunk_id: context.chunk_id.to_string(),
        exported_names,
        spec_named_export_names,
        cross_chunk_import_aliases: Vec::new(),
        file: context.entry_file.to_string(),
        owner_ids,
        residual: !plan.explicit,
        target_file: plan.target_file.clone(),
        target_path: plan.target_path.clone(),
    };
    (file, record, lowering)
}

fn anonymous_statement_comments_by_span(
    plan: &ModulePlan,
    runtime_ast: &ParsedJsModule,
) -> BTreeMap<BytePos, String> {
    plan.anonymous_statement_comments
        .iter()
        .filter_map(|(body_idx, comment)| {
            if comment.is_empty() {
                return None;
            }
            let lo = runtime_ast.module.body.get(*body_idx)?.span().lo();
            if lo == BytePos(0) {
                return None;
            }
            Some((lo, comment.clone()))
        })
        .collect()
}
