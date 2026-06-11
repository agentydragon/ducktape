//! Lower a single chunk: produce the per-chunk set of JS files (entry + extracted
//! logical modules) by running every module plan through the rename pipeline,
//! emitting cross-module imports, and naturalizing object shorthand. The chunk-
//! level orchestration that wraps this lives in mod.rs's
//! `materialize_logical_chunk`.

use std::sync::Mutex;

use rayon::prelude::*;
use swc_common::GLOBALS;
use swc_common::{BytePos, Spanned};

use super::import_emit::{
    disambiguate_import_locals, import_decl_for_plan, preserve_export_specifier_names,
    relative_source,
};
use super::scope_names::{collect_local_binding_names, collect_occupied_local_names};
use super::util::{is_valid_js_identifier, remaining_item_after_selection};
use super::*;
use crate::time_phase;

const LOWERING_FILE_PRAGMA: &str =
    "// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors";
const LOWERING_GENERATOR_HEADER: &str = "// @ducktape-generator selected_module_lowering";

pub(super) struct LoweredChunk {
    pub(super) files: Vec<JsFile>,
    pub(super) file_records: Vec<(String, FileRole)>,
    pub(super) applied: Vec<SelectedModuleLowering>,
    pub(super) timings: PhaseTimings,
}

/// Chunk-identity + file-path inputs to `lower_chunk`. Held in
/// `LowerChunkInputs::context`.
pub(super) struct LowerChunkContext<'a> {
    pub(super) artifact: &'a ChunkBundle,
    pub(super) artifact_indexes: &'a ArtifactIndexes,
    pub(super) chunk_id: &'a str,
    pub(super) source_path: &'a str,
    pub(super) entry_file: &'a str,
    pub(super) header_lines: &'a [String],
}

/// AST-side inputs: the parsed runtime module plus the top-level
/// declaration index. Held in `LowerChunkInputs::ast`.
pub(super) struct LowerChunkAst<'a> {
    pub(super) runtime_ast: &'a ParsedJsModule,
    pub(super) declarations: &'a [TopLevelDecl],
    pub(super) declaration_by_name: &'a HashMap<Id, usize>,
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
    /// In-place renames from `TransformSpec::chunk_renames`. Applied
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
        source_path,
        entry_file,
        header_lines,
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
        chunk_renames,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
    } = spec_facts;
    let is_module_owned = |name: &str| -> bool {
        binding_assignment.contains_key(&top_level_id(name, chunk_top_level_mark))
    };
    let mut timings = PhaseTimings::default();
    let selected_ordinals = time_phase!(timings, "compute_selected_ordinals", {
        compute_selected_ordinals(
            declarations,
            binding_assignment,
            anonymous_ordinal_assignment,
        )
    });

    let mut selected_by_module = vec![Vec::<ModuleItem>::new(); module_plans.len()];
    let mut selected_exports_by_module =
        vec![Option::<BTreeMap<String, String>>::None; module_plans.len()];
    time_phase!(timings, "plan_selected_exports", {
        plan_selected_exports(
            module_plans,
            &is_module_owned,
            &mut selected_exports_by_module,
        );
    });

    let mut entry_body = Vec::new();
    let import_insert_index = runtime_ast
        .module
        .body
        .iter()
        .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
        .count();
    time_phase!(timings, "split_entry_body", {
        split_entry_body(
            &runtime_ast.module.body,
            &selected_ordinals,
            anonymous_ordinal_assignment,
            binding_assignment,
            &mut entry_body,
            &mut selected_by_module,
        )
    })?;
    // Two passes: build entry imports in plan order (so the
    // first plan to claim a binding wins disambiguation), then
    // sort the resulting imports by `linker_order` so ECMA-262's
    // depth-first link traversal evaluates dependencies first.
    // Plan-order disambiguation + linker-order placement keeps
    // the import-collision contract while satisfying Lemma 2's
    // emit-side constraint. See docs/design.md "Module dep graphs"
    // and "Lemma 2".
    let build_entry_imports_started = Instant::now();
    let mut entry_imports: Vec<(usize, ModuleItem)> = Vec::new();
    let mut occupied = collect_occupied_local_names(&entry_body);
    let mut body_renames = BTreeMap::<String, String>::new();
    // Seed body_renames with `chunk_renames` entries for bindings
    // staying in entry's body (not claimed by any logical module).
    // Bindings owned by a logical module take their rename from the
    // module plan via the disambiguate-imports pass below;
    // chunk_renames entries for those bindings are silently
    // dropped here (the logical-module rename wins).
    //
    // Each accepted target name is reserved in `occupied` before the
    // import-disambiguation pass runs, so a later cross-module
    // import doesn't mint a fresh local that collides with one of
    // the chunk_renames' targets. Conflicting targets (target name
    // already taken by a body local that isn't being renamed away,
    // or by another chunk_renames entry, or invalid as an
    // identifier) bail rather than producing invalid JS silently.
    let mut renamed_away = BTreeSet::<String>::new();
    for binding in chunk_renames.keys() {
        if is_module_owned(binding) {
            continue;
        }
        renamed_away.insert(binding.clone());
    }
    // Iterate `chunk_renames` (a `HashMap`) in sorted order so the
    // collected error list and the `body_renames` insertion order
    // are stable. Collect every violation rather than `bail!`ing on
    // the first one so a spec author sees the full set in one
    // round-trip; the "duplicate target" branch in particular only
    // surfaces after `occupied.insert` returned false, so the
    // earlier-rename whose target was duplicated is implied by the
    // sort order.
    let mut sorted_renames: Vec<(&String, &String)> = chunk_renames.iter().collect();
    sorted_renames.sort_by(|a, b| a.0.cmp(b.0));
    let mut errors = Vec::<String>::new();
    for (binding, export_name) in sorted_renames {
        if is_module_owned(binding) {
            continue;
        }
        if !is_valid_js_identifier(export_name) {
            errors.push(format!(
                "chunk_renames target {export_name} for binding {binding} is not a valid JS identifier",
            ));
            continue;
        }
        if export_name != binding {
            // A body local that's also being renamed away vacates
            // its slot in `occupied` — it's safe to reuse. Anything
            // else still in `occupied` would collide.
            let target_already_taken =
                occupied.contains(export_name) && !renamed_away.contains(export_name);
            if target_already_taken {
                errors.push(format!(
                    "chunk_renames target {export_name} for binding {binding} collides with an existing top-level local",
                ));
                continue;
            }
        }
        if !occupied.insert(export_name.clone()) && export_name != binding {
            // `occupied.insert` returns false if already present;
            // for the rename-to-self case (export_name == binding)
            // that's expected. For any other case the target was
            // already chosen by a previous chunk_renames entry —
            // duplicate target.
            errors.push(format!(
                "chunk_renames target {export_name} for binding {binding} duplicates an earlier rename target",
            ));
            continue;
        }
        body_renames.insert(binding.clone(), export_name.clone());
    }
    if !errors.is_empty() {
        bail!("invalid chunk_renames spec:\n  - {}", errors.join("\n  - "));
    }
    occupied.extend(collect_local_binding_names(&entry_body));
    for (module_index, plan) in module_plans.iter().enumerate() {
        if plan.bindings.is_empty() {
            continue;
        }
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
            continue;
        }
        let mut emit_renames = BTreeMap::<String, String>::new();
        let resolved = disambiguate_import_locals(&live_bindings, &mut occupied, &mut emit_renames);
        // A rename only propagates to consumer-body references when the
        // moved decl actually belongs to this plan. Plans that listed a
        // binding without owning the decl emit a dangling import; the
        // body refs continue to resolve to whichever binding owned the
        // original local name.
        for (local, fresh) in emit_renames {
            let local_id = top_level_id(&local, chunk_top_level_mark);
            if binding_assignment.get(&local_id).copied() == Some(module_index) {
                body_renames.insert(local, fresh);
            }
        }
        entry_imports.push((
            module_index,
            import_decl_for_plan(entry_file, &plan.target_file, &resolved),
        ));
    }
    // Sort entry imports by ChunkFactorization::source_import_position, which
    // implements Lemma 2 (docs/design.md "The realizability theorem"):
    // for acyclic imports graphs the order matches linker_order
    // (dependency-first source), but for cyclic-I shapes accepted
    // by the relaxed clause-3 rule the SCC members are reverse-
    // sorted so DFS unwinds the dependency first in post-order.
    // Stable sort preserves plan-order for ties.
    entry_imports.sort_by_key(|(idx, _)| {
        factorization
            .source_import_position(ModuleId(LogicalModuleIndex(*idx)))
            .unwrap_or(usize::MAX)
    });
    let entry_imports: Vec<ModuleItem> = entry_imports.into_iter().map(|(_, it)| it).collect();
    timings.add("build_entry_imports", build_entry_imports_started.elapsed());
    let entry_binding_renames = body_renames.clone();
    if !body_renames.is_empty() {
        let rename_entry_body_started = Instant::now();
        // Re-exports `export { local }` (without `from`) collapse `local`
        // and the public exported name into a single ident. Renaming the
        // orig would also rename the public name, breaking downstream
        // consumers — so rewrite them to `export { fresh as local }`
        // before the generic renamer visits the rest.
        for item in entry_body.iter_mut() {
            preserve_export_specifier_names(item, &body_renames);
        }
        let mut renamer = IdentifierRenamer::new(&body_renames);
        for item in entry_body.iter_mut() {
            item.visit_mut_with(&mut renamer);
        }
        timings.add("rename_entry_body", rename_entry_body_started.elapsed());
    }
    if !entry_imports.is_empty() {
        let splice_entry_imports_started = Instant::now();
        let tail = entry_body.split_off(import_insert_index);
        entry_body.extend(entry_imports);
        entry_body.extend(tail);
        timings.add(
            "splice_entry_imports",
            splice_entry_imports_started.elapsed(),
        );
    }
    // Naturalize every moved body up front and cache the per-plan
    // local renames + post-naturalize body facts. Both
    // `auto_grown_residual_exports` (entry_exports_and_trim below)
    // and `plan_module_reference_needs` (the per-plan loop further
    // down) read the same `ModuleBodyFacts`; computing once here
    // eliminates a second walk over every moved body. Building the
    // cache upstream of both consumers also keeps the per-plan loop
    // free of facts-collection so it can be parallelized later
    // without re-introducing the duplicate walk.
    //
    // The naturalize pass must precede facts collection because the
    // in-place sym rewrites it performs (`plan.bindings` + heuristic
    // return-object aliases) change which `(sym, ctxt)` tuples the
    // body references; auto-grow then sees the post-naturalize set,
    // which is also what the per-plan loop sees today.
    let mut naturalized_bodies: Vec<Vec<ModuleItem>> = Vec::with_capacity(module_plans.len());
    let mut local_renames_by_module: Vec<BTreeMap<String, String>> =
        Vec::with_capacity(module_plans.len());
    let mut body_facts_by_module: Vec<ModuleBodyFacts> = Vec::with_capacity(module_plans.len());
    time_phase!(timings, "module.naturalize_body", {
        for (index, plan) in module_plans.iter().enumerate() {
            let mut body = std::mem::take(&mut selected_by_module[index]);
            let renames = naturalize_module_body(&mut body, plan);
            naturalized_bodies.push(body);
            local_renames_by_module.push(renames);
        }
    });
    time_phase!(timings, "module.collect_body_facts", {
        for body in &naturalized_bodies {
            body_facts_by_module.push(collect_module_body_facts(body));
        }
    });
    time_phase!(timings, "entry_exports_and_trim", {
        for export in entry_exports_for_moved_bindings(
            declarations,
            binding_assignment,
            &entry_binding_renames,
        ) {
            entry_body.push(export);
        }
        // Auto-grow entry's export list for any residual binding a
        // moved module body references. Without this, the per-module
        // emit path below would surface a "moved module references
        // residual entry binding(s) … not exported by entry"
        // rejection — i.e. would refuse to emit valid JS — for any
        // peel whose body happens to read a top-level binding that
        // the upstream source didn't already `export {...}`.
        // Emitting the export here makes the assignment importable
        // by construction (see docs/design.md "Valid peels and atomic
        // modules", importability clause). The grow set excludes
        // names already in entry's source-level exports.
        let auto_grow = auto_grown_residual_exports(
            &body_facts_by_module,
            declaration_by_name,
            binding_assignment,
            pre_existing_entry_exports,
            pre_existing_public_export_names,
            &entry_binding_renames,
        );
        if !auto_grow.is_empty() {
            entry_body.push(export_named_for_bindings(&auto_grow));
        }
        trim_dead_named_specifiers(&mut entry_body, &factorization.analysis.bindings);
    });
    let entry_exports_by_original_local = time_phase!(timings, "collect_entry_exports", {
        collect_entry_exports_by_original_local(
            &entry_body,
            &entry_binding_renames,
            chunk_top_level_mark,
        )
    });
    let imported_reexports_by_module = time_phase!(timings, "collect_imported_reexports", {
        collect_imported_reexports_by_module(factorization, module_plans.len())
    });
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
    // `Mutex` above. Naturalized bodies, local renames, and body
    // facts are precomputed upstream (see the
    // `module.naturalize_body` / `module.collect_body_facts` passes
    // above); the per-plan worker just consumes them.
    //
    // `PhaseTimings` is a per-iteration local; merged into the chunk-
    // level `timings` once the parallel work completes. `time_phase!`
    // requires `&mut PhaseTimings`, which the per-iter local
    // satisfies.
    //
    // `swc_common::GLOBALS` is a `scoped_tls` thread-local; it does
    // NOT carry into rayon worker threads, so we capture the parent
    // thread's `Globals` and re-set it inside each worker closure.
    // Mirrors the chunk-level `par_iter` in `lowering/mod.rs`.
    let per_plan_bodies: Vec<Vec<ModuleItem>> = std::mem::take(&mut naturalized_bodies);
    let per_plan_local_renames: Vec<BTreeMap<String, String>> =
        std::mem::take(&mut local_renames_by_module);
    let module_outputs: Vec<(
        JsFile,
        (String, FileRole),
        SelectedModuleLowering,
        PhaseTimings,
    )> = GLOBALS.with(|globals| -> Result<_> {
        per_plan_bodies
            .into_par_iter()
            .zip(per_plan_local_renames.into_par_iter())
            .zip(module_plans.par_iter())
            .zip(body_facts_by_module.par_iter())
            .enumerate()
            .map(|(index, (((body, local_renames), plan), body_facts))| {
                GLOBALS.set(globals, || {
                    lower_single_plan(LowerSinglePlanInputs {
                        index,
                        plan,
                        body,
                        local_renames,
                        body_facts,
                        factorization,
                        declaration_by_name,
                        binding_assignment,
                        runtime_import_facts,
                        entry_exports_by_original_local: &entry_exports_by_original_local,
                        imported_reexports_by_module: &imported_reexports_by_module,
                        selected_exports_by_module: &selected_exports_by_module,
                        cross_module_chunk_renames: &cross_module_chunk_renames,
                        source_import_cache: &source_import_cache,
                        chunk_id,
                        entry_file,
                        source_path,
                        chunk_top_level_mark,
                        runtime_ast,
                    })
                })
            })
            .collect()
    })?;
    for (file, record, lowering, local_timings) in module_outputs {
        files.push(file);
        file_records.push(record);
        applied.push(lowering);
        for (name, duration) in local_timings.durations {
            timings.add(name, duration);
        }
    }

    Ok(LoweredChunk {
        files,
        file_records,
        applied,
        timings,
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
) -> Result<()> {
    for (ordinal, item) in body.iter().enumerate() {
        if !selected_ordinals.contains(&ordinal) {
            entry_body.push(item.clone());
            continue;
        }
        if let Some(module_index) = anonymous_ordinal_assignment.get(&ordinal).copied() {
            selected_by_module[module_index].push(item.clone());
            continue;
        }
        let mut remaining =
            remaining_item_after_selection(item, binding_assignment, selected_by_module)?;
        entry_body.append(&mut remaining);
    }
    Ok(())
}

struct ModuleOutputContext<'a> {
    factorization: &'a ChunkFactorization,
    runtime_ast: &'a ParsedJsModule,
    chunk_top_level_mark: swc_common::Mark,
    chunk_id: &'a str,
    entry_file: &'a str,
    source_path: &'a str,
}

struct LowerSinglePlanInputs<'a> {
    index: usize,
    plan: &'a ModulePlan,
    body: Vec<ModuleItem>,
    local_renames: BTreeMap<String, String>,
    body_facts: &'a ModuleBodyFacts,
    factorization: &'a ChunkFactorization,
    declaration_by_name: &'a HashMap<Id, usize>,
    binding_assignment: &'a HashMap<Id, usize>,
    runtime_import_facts: &'a RuntimeImportFacts,
    entry_exports_by_original_local: &'a HashMap<Id, EntryExport>,
    imported_reexports_by_module: &'a [Vec<super::plan_references::ImportedReexport>],
    selected_exports_by_module: &'a [Option<BTreeMap<String, String>>],
    cross_module_chunk_renames: &'a BTreeMap<String, String>,
    source_import_cache: &'a Mutex<ArtifactSourceImportResolutionCache<'a>>,
    chunk_id: &'a str,
    entry_file: &'a str,
    source_path: &'a str,
    chunk_top_level_mark: swc_common::Mark,
    runtime_ast: &'a ParsedJsModule,
}

fn lower_single_plan(
    inputs: LowerSinglePlanInputs<'_>,
) -> Result<(
    JsFile,
    (String, FileRole),
    SelectedModuleLowering,
    PhaseTimings,
)> {
    let LowerSinglePlanInputs {
        index,
        plan,
        mut body,
        local_renames,
        body_facts,
        factorization,
        declaration_by_name,
        binding_assignment,
        runtime_import_facts,
        entry_exports_by_original_local,
        imported_reexports_by_module,
        selected_exports_by_module,
        cross_module_chunk_renames,
        source_import_cache,
        chunk_id,
        entry_file,
        source_path,
        chunk_top_level_mark,
        runtime_ast,
    } = inputs;
    let mut timings = PhaseTimings::default();
    let ModuleReferenceNeeds {
        cross_module_imports_by_provider,
        residual_entry_imports,
        missing_residual_exports,
        runtime_reimports,
        phantom_side_effect_providers,
    } = time_phase!(timings, "module.plan_references", {
        plan_module_reference_needs(
            index,
            body_facts,
            factorization,
            declaration_by_name,
            binding_assignment,
            entry_exports_by_original_local,
            RuntimeImportLookup {
                imports: runtime_import_facts,
                heuristic_renames: &local_renames,
            },
        )
    });
    let mut module_import_renames = BTreeMap::<String, String>::new();
    let mut module_import_locals = collect_local_binding_names(&body);
    let mut module_imports = time_phase!(timings, "module.build_cross_imports", {
        cross_module_imports_for_plan(
            &plan.target_file,
            cross_module_imports_by_provider,
            factorization,
            &mut module_import_locals,
            &mut module_import_renames,
        )
    });
    // Phantom side-effect imports surface at-init-promotion-derived
    // constraining edges as real ESM imports so the linker's DFS
    // visits the provider modules as dependencies. Prepended so
    // they sort before residual-entry and runtime re-imports, all
    // of which would short-circuit the cycle when the residual is
    // mid-evaluation. See `phantom_side_effect_imports` and
    // `accepted_spec_runs_under_node_test::early_entry_importer_…`.
    let phantom_imports = time_phase!(timings, "module.build_phantom_imports", {
        phantom_side_effect_imports(
            &plan.target_file,
            phantom_side_effect_providers,
            factorization,
        )
    });
    let mut residual_entry_imports = time_phase!(timings, "module.build_residual_imports", {
        residual_entry_imports_for_moved_body(
            &plan.id,
            entry_file,
            &plan.target_file,
            residual_entry_imports,
            missing_residual_exports,
            &mut module_import_locals,
            &mut module_import_renames,
        )
    })?;
    if !module_import_renames.is_empty() {
        let mut renamer = IdentifierRenamer::new(&module_import_renames);
        for item in body.iter_mut() {
            item.visit_mut_with(&mut renamer);
        }
    }
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
    let mut runtime_reimports = time_phase!(timings, "module.build_runtime_reimports", {
        let mut cache = source_import_cache
            .lock()
            .expect("source_import_cache poisoned");
        source_chunk_imports_for_moved_body(
            &mut cache,
            chunk_id,
            entry_file,
            &plan.target_file,
            runtime_reimports,
        )
    })?;
    module_imports.append(&mut residual_entry_imports);
    module_imports.append(&mut runtime_reimports);
    // Phantom side-effect imports go FIRST in the emitted module
    // so the linker DFS-recurses into the providers before the
    // residual/entry import (which would short-circuit when the
    // residual is mid-evaluation).
    let mut combined = phantom_imports;
    combined.append(&mut module_imports);
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
        time_phase!(timings, "module.rename_chunk_renames", {
            let mut renamer = IdentifierRenamer::new(cross_module_chunk_renames);
            for item in body.iter_mut() {
                item.visit_mut_with(&mut renamer);
            }
        });
    }
    time_phase!(timings, "module.rewrite_runtime_sources", {
        rewrite_runtime_sources_for_target(&mut body, chunk_id, entry_file, &plan.target_file);
    });
    // ImportSpecifier-bound members (`BindingKind::Imported` in
    // `factorization.analysis.bindings`): for each `Imported` binding whose
    // `re_exported_by` map names this module, emit a re-import
    // (using the local name as the alias) plus mirror the
    // public-name export. Per-destination relative paths are
    // computed here so multiple modules at different output
    // depths each get a correctly-relativised path.
    let import_member_exports = time_phase!(timings, "module.imported_reexports", {
        let mut import_member_exports = BTreeMap::<String, String>::new();
        let reexports = &imported_reexports_by_module[index];
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
        import_member_exports
    });
    time_phase!(timings, "module.final_exports", {
        if let Some(exports) = &selected_exports_by_module[index] {
            let mut exports = final_module_exports(exports, &local_renames);
            exports.extend(
                import_member_exports
                    .iter()
                    .map(|(k, v)| (k.clone(), v.clone())),
            );
            body.push(export_named_for_bindings(&exports));
        } else if !import_member_exports.is_empty() {
            body.push(export_named_for_bindings(&import_member_exports));
        }
    });
    let (file, record, lowering) = time_phase!(timings, "module.build_output_records", {
        let output_context = ModuleOutputContext {
            factorization,
            runtime_ast,
            chunk_top_level_mark,
            chunk_id,
            entry_file,
            source_path,
        };
        build_module_output(plan, body, &output_context)
    });
    Ok((file, record, lowering, timings))
}

fn build_module_output(
    plan: &ModulePlan,
    body: Vec<ModuleItem>,
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
        binding_comments: plan.binding_comments.clone(),
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
        file: context.entry_file.to_string(),
        id: plan.id.clone(),
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
