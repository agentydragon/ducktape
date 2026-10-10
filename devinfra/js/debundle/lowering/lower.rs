//! Lower a single chunk: produce the per-chunk set of JS files (entry + extracted
//! logical modules) by running every module plan through the rename pipeline,
//! emitting cross-module imports, and naturalizing object shorthand. The chunk-
//! level planning and gate checks that wrap this live in `materialize/`.

use js_ast::import_decl_module_item;
use std::sync::Mutex;

use super::module_output::{LoweredModuleOutput, ModuleEmissionInputs, emit_module};
use swc_common::{DUMMY_SP, GLOBALS};

use super::chunk_renames::CHUNK_RENAMES_CONTRIBUTOR;
use super::imports::import_emit::{
    disambiguate_import_locals, import_decl_for_plan, preserve_export_specifier_names,
    relative_source,
};

use super::scope_names::{
    collect_local_binding_names, collect_nested_binding_names, collect_occupied_local_names,
};
use super::util::remaining_item_after_selection;
use crate::chunk_ast::{TopLevelDecl, top_level_declaration_names};
use crate::exports::{
    ExportGrowthFacts, auto_grown_residual_exports, entry_exports_for_moved_bindings,
    export_named_for_bindings, trim_dead_named_specifiers,
};
use crate::imports::{
    ArtifactSourceImportResolutionCache, RuntimeImportFacts, VendorReimportOracle,
    collect_entry_exports_by_original_local, collect_imported_reexports_by_module,
};
use crate::naturalize::NaturalizedModuleBody;
use crate::plans::ModulePlan;
use crate::rename_ledger::{
    RenameIntent, RenameLedger, RenameOrigin, RenameScope, ScopeOccupancy, SealValidation,
    SealedRenames,
};
use crate::visitors::{IdentifierRenamer, RenameCaptureProbe};
use analysis::{LogicalModuleIndex, ModuleId, top_level_id};
use anyhow::Result;
use artifact::{
    ArtifactIndexes, ChunkBundle, ChunkId, FileMetadata, FileRole, JsFile, JsFileBody,
    SelectedModuleLowering,
};
use gate::ChunkFactorization;
use js_ast::ParsedJsModule;
use rayon::prelude::*;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMutWith, VisitWith};

pub(super) const ENTRY_IMPORT_LOCAL_CONTRIBUTOR: &str = "entry import-local disambiguation";

pub(super) struct LoweredChunk {
    pub(super) files: Vec<JsFile>,
    pub(super) file_records: Vec<(String, FileRole)>,
    pub(super) applied: Vec<SelectedModuleLowering>,
    /// Per-symbol vendor-swap rewrite counts applied at construction
    /// time across this chunk's module bodies (see
    /// `imports::vendor_imports::plan_vendor_reimports`).
    pub(super) vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
}

/// Chunk-identity + file-path inputs to `lower_chunk`. Held in
/// `LowerChunkInputs::context`.
pub(super) struct LowerChunkContext<'a> {
    pub(super) artifact: &'a ChunkBundle,
    pub(super) artifact_indexes: &'a ArtifactIndexes,
    pub(super) chunk_id: &'a str,
    pub(super) chunk_id_interned: ChunkId,
    pub(super) source_path: &'a str,
    pub(super) entry_file: &'a str,
    pub(super) header_lines: &'a [String],
    pub(super) vendor_import_oracle: Option<&'a VendorReimportOracle<'a>>,
}

/// AST-side inputs: the parsed runtime module plus the top-level
/// declaration index. Held in `LowerChunkInputs::ast`.
pub(super) struct LowerChunkAst<'a> {
    pub(super) runtime_ast: &'a ParsedJsModule,
    pub(super) declarations: &'a [TopLevelDecl],
    pub(super) declaration_by_name: &'a HashMap<Id, Vec<usize>>,
    pub(super) chunk_top_level_mark: swc_common::Mark,
}

/// Plan-side inputs: the per-chunk module plans + the binding /
/// anonymous-ordinal assignment + the realizability factorization.
/// Held in `LowerChunkInputs::plan`.
pub(super) struct LowerChunkPlan<'a> {
    pub(super) module_plans: &'a [ModulePlan],
    pub(super) binding_assignment: &'a HashMap<Id, usize>,
    /// Top-level statement ordinal → module_plan index for owners
    /// the spec claimed as anonymous-statement members. See
    /// `ModulePlan::anonymous_statement_ordinals`.
    pub(super) anonymous_ordinal_assignment: &'a BTreeMap<usize, usize>,
    pub(super) factorization: &'a ChunkFactorization,
}

/// Spec-derived + chunk-AST-derived facts the lowerer consults at
/// emission time. Held in `LowerChunkInputs::spec_facts`.
pub(super) struct LowerChunkSpecFacts<'a> {
    pub(super) runtime_import_facts: &'a RuntimeImportFacts,
    /// The chunk's sealed rename ledger (explicit contributors collected
    /// and conflict-validated in `materialize_logical_chunk`). The
    /// per-plan naturalize pass below queries its Module-scope maps;
    /// `chunk_renames` is its Chunk-scope projection.
    pub(super) sealed_renames: &'a SealedRenames,
    /// In-place renames from `TransformSpec::chunk_renames` — the sealed
    /// ledger's Chunk-scope projection. Applied
    /// to bindings staying in entry's body — i.e. those *not* in
    /// `binding_assignment`. Bindings claimed by a logical module
    /// take their rename from the module plan; entries here for
    /// those bindings are silently dropped. Iteration order is
    /// undefined; the validation pass sorts by binding name before
    /// iterating so any spec errors are deterministic.
    pub(super) chunk_renames: &'a HashMap<String, String>,
    /// Bindings the source chunk's entry exports verbatim
    /// (`record_pre_existing_named_exports`). Consulted by
    /// `auto_grown_residual_exports` so the auto-grow pass doesn't
    /// emit a `Duplicate export of 'name'` clash with an existing
    /// source export.
    pub(super) pre_existing_entry_exports: &'a HashSet<Id>,
    /// **Public** names entry already uses — the `exported` side of
    /// the same `export { … }` block plus the declared name of any
    /// `export const/function/class` declaration. Consulted by
    /// `auto_grown_residual_exports` so the grown public name
    /// suffix-mints (`<base>$1`) past any source-level alias
    /// collision instead of emitting a duplicate.
    pub(super) pre_existing_public_export_names: &'a HashSet<String>,
}

pub(super) struct LowerChunkInputs<'a> {
    pub(super) context: LowerChunkContext<'a>,
    pub(super) ast: LowerChunkAst<'a>,
    pub(super) plan: LowerChunkPlan<'a>,
    pub(super) spec_facts: LowerChunkSpecFacts<'a>,
}

pub(super) fn lower_chunk(inputs: LowerChunkInputs<'_>) -> Result<LoweredChunk> {
    let LowerChunkInputs {
        context,
        ast,
        plan,
        spec_facts,
    } = inputs;
    let LowerChunkContext {
        artifact,
        artifact_indexes,
        chunk_id,
        chunk_id_interned,
        source_path,
        entry_file,
        header_lines,
        vendor_import_oracle,
    } = context;
    let LowerChunkAst {
        runtime_ast,
        declarations,
        declaration_by_name,
        chunk_top_level_mark,
    } = ast;
    let LowerChunkPlan {
        module_plans,
        binding_assignment,
        anonymous_ordinal_assignment,
        factorization,
    } = plan;
    let LowerChunkSpecFacts {
        runtime_import_facts,
        sealed_renames,
        chunk_renames,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
    } = spec_facts;
    let is_module_owned = |name: &str| -> bool {
        binding_assignment.contains_key(&top_level_id(name, chunk_top_level_mark))
    };
    let selected_ordinals = compute_selected_ordinals(
        declarations,
        binding_assignment,
        anonymous_ordinal_assignment,
    );

    let mut selected_by_module = vec![Vec::<ModuleItem>::new(); module_plans.len()];
    let mut selected_exports_by_module =
        vec![Option::<BTreeMap<String, String>>::None; module_plans.len()];
    plan_selected_exports(
        module_plans,
        &is_module_owned,
        &mut selected_exports_by_module,
    );

    let mut entry_body = Vec::new();
    split_entry_body(
        &runtime_ast.module.body,
        &selected_ordinals,
        anonymous_ordinal_assignment,
        binding_assignment,
        &mut entry_body,
        &mut selected_by_module,
    );
    // Two passes: build entry imports in plan order (so the
    // first plan to claim a binding wins disambiguation), then
    // order them through the shared `EsmImportOrder` so ECMA-262's
    // depth-first link traversal evaluates dependencies first.
    // Plan-order disambiguation + shared-order placement keeps
    // the import-collision contract while satisfying Lemma 2's
    // emit-side constraint. See docs/design.md "Module dep graphs"
    // and "Lemma 2".
    let mut entry_imports: Vec<(ModuleId, ModuleItem)> = Vec::new();
    // Entry-side renames flow through ONE per-chunk ledger sealed once:
    // the chunk_renames entries staying in entry (Explicit, Chunk scope),
    // the entry import-local mints (ImportInduced, Chunk scope), and the
    // auto-grown residual public exports (ImportInduced,
    // EntryPublicExports scope — a separate namespace from local-binding
    // renames). The Chunk-scope contributors' source sets are disjoint by
    // construction — chunk_renames seeding skips module-owned bindings,
    // mints fire only for module-owned bindings — so a seal conflict can
    // only come from one contributor disagreeing with itself.
    //
    // Occupancy facts for seal validation: the post-split entry body's
    // top-level names (root) and everything bound below them (nested).
    // Seal rejects chunk_renames targets colliding at the root level —
    // collecting every violation in one error — and asserts mints stayed
    // clear of both levels; nested-capture facts come from the read-only
    // capture probe below.
    let entry_root_names = collect_occupied_local_names(&entry_body);
    let entry_nested_names = collect_nested_binding_names(&entry_body);
    let mut entry_ledger = RenameLedger::default();
    entry_ledger.seed_taken(RenameScope::Chunk, entry_root_names.iter().cloned());
    // Submit `chunk_renames` intents for bindings staying in entry's
    // body (not claimed by any logical module). Bindings owned by a
    // logical module take their rename from the module plan via the
    // disambiguate-imports pass below; chunk_renames entries for those
    // bindings are silently dropped here (the logical-module rename
    // wins).
    //
    // Each target name is claimed before the import-disambiguation pass
    // runs, so a later cross-module import doesn't mint a fresh local
    // that collides with one of the chunk_renames' targets. Iterate
    // `chunk_renames` (a `HashMap`) in sorted order so the ledger's
    // intent order is stable.
    let mut sorted_renames: Vec<(&String, &String)> = chunk_renames.iter().collect();
    sorted_renames.sort_by(|a, b| a.0.cmp(b.0));
    for (binding, export_name) in sorted_renames {
        if is_module_owned(binding) {
            continue;
        }
        entry_ledger.claim(RenameScope::Chunk, export_name);
        entry_ledger.submit(RenameIntent {
            scope: RenameScope::Chunk,
            from: top_level_id(binding, chunk_top_level_mark),
            to: export_name.as_str().into(),
            origin: RenameOrigin::Explicit {
                contributor: CHUNK_RENAMES_CONTRIBUTOR,
            },
        });
    }
    // Mints must additionally avoid every nested binding name, or the
    // follow-up body rewrite could capture references meant to resolve
    // to the import.
    entry_ledger.seed_taken(RenameScope::Chunk, collect_local_binding_names(&entry_body));
    for (module_index, plan) in module_plans.iter().enumerate() {
        // Drop bindings that don't exist anywhere (no entry in
        // `binding_assignment`). Bindings owned by another plan stay
        // in the import — they're a separate "two plans claim the
        // same binding" disambiguation case handled by
        // `disambiguate_import_locals`.
        let live_bindings: BTreeMap<String, String> = plan
            .bindings
            .iter()
            .filter(|(name, _)| is_module_owned(name))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        if live_bindings.is_empty() {
            // A plan with no importable bindings can still carry
            // emitted content (anonymous claimed statements). The
            // entry must still load it — a module nothing imports
            // never evaluates, silently dropping its side effects —
            // so emit a side-effect-only `import "./<plan>.js";`.
            // Included in the shared entry ordering below, exactly
            // like the gate simulator's universal residual edges.
            if !selected_by_module[module_index].is_empty() {
                entry_imports.push((
                    ModuleId(LogicalModuleIndex(module_index)),
                    import_decl_module_item(
                        Vec::new(),
                        &relative_source(entry_file, &plan.target_file),
                    ),
                ));
            }
            continue;
        }
        let mut emit_renames = BTreeMap::<String, String>::new();
        let resolved = disambiguate_import_locals(
            &live_bindings,
            &mut entry_ledger,
            RenameScope::Chunk,
            &mut emit_renames,
        );
        // A rename only propagates to consumer-body references when the
        // moved decl actually belongs to this plan. Plans that listed a
        // binding without owning the decl emit a dangling import; the
        // body refs continue to resolve to whichever binding owned the
        // original local name.
        for (local, fresh) in emit_renames {
            let local_id = top_level_id(&local, chunk_top_level_mark);
            if binding_assignment.get(&local_id).copied() == Some(module_index) {
                entry_ledger.submit(RenameIntent {
                    scope: RenameScope::Chunk,
                    from: local_id,
                    to: fresh.as_str().into(),
                    origin: RenameOrigin::ImportInduced {
                        contributor: ENTRY_IMPORT_LOCAL_CONTRIBUTOR,
                    },
                });
            }
        }
        entry_imports.push((
            ModuleId(LogicalModuleIndex(module_index)),
            import_decl_for_plan(entry_file, &plan.target_file, &resolved),
        ));
    }
    // Order entry imports through the shared `EsmImportOrder`
    // (Lemma 2, docs/design.md "The realizability theorem"): for
    // acyclic imports graphs the order matches linker_order
    // (dependency-first source), but for cyclic-I shapes accepted
    // by the relaxed clause-3 rule the SCC members are reverse-
    // sorted so DFS unwinds the dependency first in post-order.
    // The gate's evaluation simulator sorts residual's DFS
    // neighbors with the same call, so the emitted entry order and
    // the simulated order are structurally identical.
    factorization
        .import_order()
        .sort_entry_imports(&mut entry_imports);
    let entry_imports: Vec<ModuleItem> = entry_imports.into_iter().map(|(_, it)| it).collect();
    // Capture probe: a read-only walk of the un-renamed entry body with
    // the pending Chunk-scope map, reporting the precise capture facts
    // seal validates (a target bound nested is harmless when the source
    // is shadowed there — only a scope-aware walk can decide that). The
    // probe replaces the pre-seal trial application; the body is mutated
    // only by the post-seal executor below.
    let entry_candidate_renames = entry_ledger.pending_renames_by_name(&RenameScope::Chunk);
    let mut entry_captured = BTreeSet::new();
    if !entry_candidate_renames.is_empty() {
        let mut probe = RenameCaptureProbe::new(&entry_candidate_renames);
        for item in entry_body.iter() {
            item.visit_with(&mut probe);
        }
        entry_captured = probe.captured;
    }
    // Naturalize before collecting facts: both entry export growth and module
    // emission must see the same post-rename references and their rename maps.
    let naturalized_modules = selected_by_module
        .into_iter()
        .zip(module_plans)
        .enumerate()
        .map(|(index, (body, plan))| {
            let module = ModuleId::logical(index);
            NaturalizedModuleBody::prepare(
                body,
                plan,
                module,
                sealed_renames.module_renames_by_name(module),
                chunk_top_level_mark,
            )
        })
        .collect::<Result<Vec<_>>>()?;
    // Auto-grow entry's export list for any residual binding a
    // moved module body references. Without this, the per-module
    // emit path below would surface a "moved module references
    // residual entry binding(s) … not exported by entry"
    // rejection — i.e. would refuse to emit valid JS — for any
    // peel whose body happens to read a top-level binding that
    // the upstream source didn't already `export {...}`.
    // Emitting the export makes the assignment importable
    // by construction (see docs/design.md "Valid peels and atomic
    // modules", importability clause). The grow set excludes
    // names already in entry's source-level exports.
    //
    // The mints land in the chunk ledger's `EntryPublicExports` scope —
    // a separate namespace from the Chunk-scope local renames — seeded
    // with the source-level public names so the grown names suffix past
    // them. Intents are keyed by the residual binding's ORIGINAL name;
    // the emitted clause remaps to entry's post-rename local below.
    let entry_declared_names: HashSet<String> = entry_body
        .iter()
        .flat_map(|item| top_level_declaration_names(item).0)
        .collect();
    entry_ledger.seed_taken(
        RenameScope::EntryPublicExports,
        pre_existing_public_export_names.iter().cloned(),
    );
    auto_grown_residual_exports(
        naturalized_modules.iter().map(|module| &module.facts),
        &ExportGrowthFacts {
            declaration_by_name,
            binding_assignment,
            pre_existing_entry_exports,
            entry_declared_names: &entry_declared_names,
        },
        chunk_top_level_mark,
        &mut entry_ledger,
    );
    // Seal the chunk ledger: the single validation point for every
    // entry-side rename. Chunk scope: conflicting targets (target name
    // already taken by a body local that isn't being renamed away, or
    // chosen by another chunk_renames entry, invalid as an identifier,
    // or captured by a nested binding per the probe above) bail here
    // with every violation listed, rather than producing invalid JS
    // silently. EntryPublicExports scope: a single contributor over a
    // set-deduped source set cannot conflict; the occupancy check
    // asserts the mints stayed clear of pre-existing public names.
    let sealed_entry_renames = entry_ledger.seal(&SealValidation {
        occupancy: BTreeMap::from([
            (
                RenameScope::Chunk,
                ScopeOccupancy::Body {
                    label: chunk_id.to_string(),
                    root: entry_root_names,
                    nested: entry_nested_names,
                    captured: entry_captured,
                },
            ),
            (
                RenameScope::EntryPublicExports,
                ScopeOccupancy::Body {
                    label: chunk_id.to_string(),
                    root: pre_existing_public_export_names.iter().cloned().collect(),
                    nested: BTreeSet::new(),
                    captured: BTreeSet::new(),
                },
            ),
        ]),
        reserved: BTreeSet::new(),
    })?;
    let entry_binding_renames = sealed_entry_renames.scope_renames_by_name(&RenameScope::Chunk);
    debug_assert_eq!(
        entry_binding_renames, entry_candidate_renames,
        "sealed entry renames diverged from the pending map the capture probe walked",
    );
    // Execute once: the single rename pass over the entry body, applying
    // the sealed Chunk-scope map. Re-exports `export { local }` (without
    // `from`) collapse `local` and the public exported name into a single
    // ident; renaming the orig would also rename the public name, so
    // rewrite them to `export { fresh as local }` before the generic
    // renamer visits the rest. The probe above already fed seal the walk's
    // capture verdict, so a capture here means probe and executor diverged.
    if !entry_binding_renames.is_empty() {
        for item in entry_body.iter_mut() {
            preserve_export_specifier_names(item, &entry_binding_renames);
        }
        let mut renamer = IdentifierRenamer::new(&entry_binding_renames);
        for item in entry_body.iter_mut() {
            item.visit_mut_with(&mut renamer);
        }
        debug_assert!(
            renamer.captured.is_empty(),
            "entry rename executor captured despite seal validation: {:?}",
            renamer.captured,
        );
    }
    if !entry_imports.is_empty() {
        let import_insert_index = entry_body
            .iter()
            .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
            .count();
        let tail = entry_body.split_off(import_insert_index);
        entry_body.extend(entry_imports);
        entry_body.extend(tail);
    }
    for export in
        entry_exports_for_moved_bindings(declarations, binding_assignment, &entry_binding_renames)
    {
        entry_body.push(export);
    }
    // The sealed EntryPublicExports map is keyed by the residual
    // binding's original name; the emitted export clause is keyed by
    // entry's post-rename local, so remap through
    // `entry_binding_renames` at application.
    let auto_grow: BTreeMap<String, String> = sealed_entry_renames
        .scope_renames_by_name(&RenameScope::EntryPublicExports)
        .into_iter()
        .map(|(original, public_name)| {
            (
                entry_binding_renames
                    .get(&original)
                    .cloned()
                    .unwrap_or(original),
                public_name,
            )
        })
        .collect();
    if !auto_grow.is_empty() {
        entry_body.push(export_named_for_bindings(&auto_grow));
    }
    // A readable name assigned to a chunk-exported binding is additive:
    // retain the source chunk's original public export (needed by chunks
    // outside the pipeline, dynamic imports, and namespace consumers) and
    // expose the same live binding under its readable name as well.
    // Candidates are ordered by name then source binding so collisions are
    // deterministic; a collision simply leaves that alias unavailable.
    let mut readable_export_candidates = Vec::<(String, String)>::new();
    for plan in module_plans {
        for (binding, readable) in &plan.bindings {
            if plan.spec_named_bindings.contains(binding)
                && pre_existing_entry_exports.contains(&top_level_id(binding, chunk_top_level_mark))
            {
                readable_export_candidates.push((readable.clone(), binding.clone()));
            }
        }
    }
    readable_export_candidates.sort();
    let mut taken_public_names = pre_existing_public_export_names.clone();
    taken_public_names.extend(auto_grow.values().cloned());
    let mut readable_exports = BTreeMap::new();
    for (readable, binding) in readable_export_candidates {
        if !taken_public_names.insert(readable.clone()) {
            continue;
        }
        let local = entry_binding_renames
            .get(&binding)
            .cloned()
            .unwrap_or(binding);
        readable_exports.insert(local, readable);
    }
    let mut cross_chunk_import_aliases = Vec::<(String, String)>::new();
    if !readable_exports.is_empty() {
        // Record only aliases that were actually emitted, associated with
        // the pre-existing public names of the same local binding. A name
        // that merely happens to equal another binding's spec name is not
        // enough to authorize rewriting its imports.
        for item in &entry_body {
            let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) = item else {
                continue;
            };
            if export.src.is_some() {
                continue;
            }
            for specifier in &export.specifiers {
                let ExportSpecifier::Named(named) = specifier else {
                    continue;
                };
                let ModuleExportName::Ident(local) = &named.orig else {
                    continue;
                };
                let Some(readable) = readable_exports.get(local.sym.as_ref()) else {
                    continue;
                };
                let public = named
                    .exported
                    .as_ref()
                    .map(binding_targets::module_export_name)
                    .unwrap_or_else(|| local.sym.to_string());
                cross_chunk_import_aliases.push((public, readable.clone()));
            }
        }
        cross_chunk_import_aliases.sort();
        cross_chunk_import_aliases.dedup();
        entry_body.push(export_named_for_bindings(&readable_exports));
    }
    trim_dead_named_specifiers(&mut entry_body, factorization.analysis.bindings());
    let entry_exports_by_original_local = collect_entry_exports_by_original_local(
        &entry_body,
        &entry_binding_renames,
        chunk_top_level_mark,
    );
    let imported_reexports_by_module =
        collect_imported_reexports_by_module(factorization, module_plans.len());
    let source_import_cache = Mutex::new(ArtifactSourceImportResolutionCache::new(
        artifact,
        artifact_indexes,
    ));

    let mut files = vec![JsFile {
        path: entry_file.to_string(),
        body: JsFileBody::Ast(ParsedJsModule {
            cm: runtime_ast.cm.clone(),
            module: Module {
                span: DUMMY_SP,
                body: entry_body,
                shebang: None,
            },
            unresolved_mark: runtime_ast.unresolved_mark,
            top_level_mark: runtime_ast.top_level_mark,
        }),
        header_lines: header_lines.to_vec(),
        binding_comments: BTreeMap::new(),
        leading_item_comments: BTreeMap::new(),
        metadata: FileMetadata {
            chunk_id: chunk_id.to_string(),
            chunk_file: entry_file.to_string(),
            role: FileRole::Entry,
            source_path: source_path.to_string(),
        },
    }];
    let mut file_records = vec![(entry_file.to_string(), FileRole::Entry)];
    let mut applied = Vec::new();

    // Filter chunk_renames down to entries the per-module emit path
    // should apply: bindings *not* claimed by any logical module.
    // Claimed bindings get their rename from the module plan
    // (handled via `disambiguate_import_locals` for cross-module
    // imports of the binding); the chunk_renames entry is dropped
    // for those. Mirrors the residual-side rule on body_renames
    // seeding above. The map is empty for chunks with no
    // chunk_renames; the per-module renamer is then a no-op.
    let cross_module_chunk_renames: BTreeMap<String, String> = chunk_renames
        .iter()
        .filter(|(binding, _)| !is_module_owned(binding))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();

    // Per-plan lowering: each plan's output (`JsFile`, file record,
    // `SelectedModuleLowering`) is independent — the only shared
    // mutable state the loop body touches is `source_import_cache`
    // (memoization for source-chunk import resolution), wrapped in a
    // `Mutex` above. Each worker consumes one naturalized body with its
    // matching rename maps and facts, prepared upstream.
    //
    // `swc_common::GLOBALS` is a `scoped_tls` thread-local; it does
    // NOT carry into rayon worker threads, so we capture the parent
    // thread's `Globals` and re-set it inside each worker closure.
    // Mirrors the chunk-level `par_iter` in `lowering/mod.rs`.
    let module_outputs: Vec<LoweredModuleOutput> = GLOBALS.with(|globals| -> Result<_> {
        naturalized_modules
            .into_par_iter()
            .zip(module_plans.par_iter())
            .enumerate()
            .map(|(index, (module, plan))| {
                GLOBALS.set(globals, || {
                    emit_module(ModuleEmissionInputs {
                        index,
                        plan,
                        naturalized: module,
                        factorization,
                        declaration_by_name,
                        binding_assignment,
                        runtime_import_facts,
                        entry_exports_by_original_local: &entry_exports_by_original_local,
                        imported_reexports: &imported_reexports_by_module[index],
                        selected_exports: selected_exports_by_module[index].as_ref(),
                        cross_module_chunk_renames: &cross_module_chunk_renames,
                        source_import_cache: &source_import_cache,
                        chunk_id,
                        chunk_id_interned,
                        entry_file,
                        source_path,
                        chunk_top_level_mark,
                        runtime_ast,
                        vendor_import_oracle,
                    })
                })
            })
            .collect()
    })?;
    let mut vendor_reference_rewrites = BTreeMap::<(ChunkId, String), usize>::new();
    for output in module_outputs {
        files.push(output.file);
        file_records.push(output.record);
        let mut lowering = output.lowering;
        lowering.cross_chunk_import_aliases = cross_chunk_import_aliases.clone();
        applied.push(lowering);
        for (key, count) in output.vendor_reference_rewrites {
            *vendor_reference_rewrites.entry(key).or_insert(0) += count;
        }
    }

    Ok(LoweredChunk {
        files,
        file_records,
        applied,
        vendor_reference_rewrites,
    })
}

fn compute_selected_ordinals(
    declarations: &[TopLevelDecl],
    binding_assignment: &HashMap<Id, usize>,
    anonymous_ordinal_assignment: &BTreeMap<usize, usize>,
) -> BTreeSet<usize> {
    let mut selected_ordinals = BTreeSet::new();
    for decl in declarations {
        if decl
            .bindings
            .iter()
            .any(|(_, id)| binding_assignment.contains_key(id))
        {
            selected_ordinals.insert(decl.ordinal);
        }
    }
    for ordinal in anonymous_ordinal_assignment.keys() {
        selected_ordinals.insert(*ordinal);
    }
    selected_ordinals
}

fn plan_selected_exports(
    module_plans: &[ModulePlan],
    is_module_owned: &impl Fn(&str) -> bool,
    selected_exports_by_module: &mut [Option<BTreeMap<String, String>>],
) {
    for (module_index, plan) in module_plans.iter().enumerate() {
        if plan.bindings.is_empty() {
            continue;
        }
        let exports: BTreeMap<String, String> = plan
            .bindings
            .iter()
            .filter(|(name, _)| is_module_owned(name))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        if !exports.is_empty() {
            selected_exports_by_module[module_index] = Some(exports);
        }
    }
}

fn split_entry_body(
    body: &[ModuleItem],
    selected_ordinals: &BTreeSet<usize>,
    anonymous_ordinal_assignment: &BTreeMap<usize, usize>,
    binding_assignment: &HashMap<Id, usize>,
    entry_body: &mut Vec<ModuleItem>,
    selected_by_module: &mut [Vec<ModuleItem>],
) {
    for (ordinal, item) in body.iter().enumerate() {
        if !selected_ordinals.contains(&ordinal) {
            entry_body.push(item.clone());
            continue;
        }
        if let Some(module_index) = anonymous_ordinal_assignment.get(&ordinal).copied() {
            selected_by_module[module_index].push(item.clone());
            // Local named exports define the source chunk's public surface.
            // An anonymous claim may move their source directive alongside
            // the binding, but entry still needs a facade for outside chunks
            // and dynamic importers. Its planned import of the moved binding
            // supplies the local name used by this unchanged export clause.
            if matches!(
                item,
                ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) if named.src.is_none()
            ) {
                entry_body.push(item.clone());
            }
            continue;
        }
        entry_body.extend(remaining_item_after_selection(
            item,
            binding_assignment,
            selected_by_module,
        ));
    }
}
