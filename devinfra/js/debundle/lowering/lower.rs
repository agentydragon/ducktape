//! Lower a single chunk: produce the per-chunk set of JS files (entry + extracted
//! logical modules) by running every module plan through the rename pipeline,
//! emitting cross-module imports, and naturalizing object shorthand. The chunk-
//! level orchestration that wraps this lives in mod.rs's
//! `materialize_logical_chunk`.

use super::chunk_renames::{
    disambiguate_import_locals_via_plan, new_chunk_plan, submit_chunk_renames,
};
use super::lowering_execute::{apply_chunk_renames_to_items, apply_plan_renames_and_naturalize};
use super::lowering_plan::Scope;
use super::util::{
    collect_local_binding_names, collect_occupied_local_names, import_decl_for_plan,
    relative_source, remaining_item_after_selection,
};
use super::*;
use crate::time_phase;
use swc_atoms::Atom;

pub(super) struct LoweredChunk {
    pub(super) files: Vec<JsFile>,
    pub(super) file_records: Vec<(String, FileRole)>,
    pub(super) applied: Vec<SelectedModuleLowering>,
    pub(super) timings: PhaseTimings,
}

pub(super) struct LowerChunkInputs<'a> {
    pub(super) artifact: &'a ChunkBundle,
    pub(super) artifact_indexes: &'a ArtifactIndexes,
    pub(super) runtime_ast: &'a ParsedJsModule,
    pub(super) header_lines: &'a [String],
    pub(super) entry_file: &'a str,
    pub(super) chunk_id: &'a str,
    pub(super) source_path: &'a str,
    pub(super) declarations: &'a [TopLevelDecl],
    pub(super) declaration_by_name: &'a HashMap<Id, usize>,
    pub(super) module_plans: &'a [ModulePlan],
    pub(super) binding_assignment: &'a HashMap<Id, usize>,
    pub(super) chunk_top_level_mark: swc_common::Mark,
    /// Top-level statement ordinal → module_plan index for owners
    /// the spec claimed as anonymous-statement members. See
    /// `ModulePlan::anonymous_statement_ordinals`.
    pub(super) anonymous_ordinal_assignment: &'a BTreeMap<usize, usize>,
    pub(super) factorization: &'a ChunkFactorization,
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

pub(super) fn lower_chunk(inputs: LowerChunkInputs<'_>) -> Result<LoweredChunk> {
    let LowerChunkInputs {
        artifact,
        artifact_indexes,
        runtime_ast,
        header_lines,
        entry_file,
        chunk_id,
        source_path,
        declarations,
        declaration_by_name,
        module_plans,
        binding_assignment,
        chunk_top_level_mark,
        anonymous_ordinal_assignment,
        factorization,
        runtime_import_facts,
        chunk_renames,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
    } = inputs;
    let mut timings = PhaseTimings::default();
    let selected_ordinals = time_phase!(timings, "compute_selected_ordinals", {
        let mut selected_ordinals = BTreeSet::new();
        for decl in declarations {
            if decl
                .ids
                .iter()
                .any(|id| binding_assignment.contains_key(id))
            {
                selected_ordinals.insert(decl.ordinal);
            }
        }
        for ordinal in anonymous_ordinal_assignment.keys() {
            selected_ordinals.insert(*ordinal);
        }
        selected_ordinals
    });

    let mut selected_by_module = vec![Vec::<ModuleItem>::new(); module_plans.len()];
    let mut selected_exports_by_module =
        vec![Option::<BTreeMap<String, String>>::None; module_plans.len()];
    time_phase!(timings, "plan_selected_exports", {
        for (module_index, plan) in module_plans.iter().enumerate() {
            if plan.bindings.is_empty() {
                continue;
            }
            // Drop bindings that don't exist anywhere (no entry in
            // `binding_assignment`). Without this, a stale spec entry
            // for a binding that is not a top-level decl in the chunk
            // would emit `export { <renamed> }` with no backing decl
            // and Node bails at module load with `SyntaxError: Export
            // '<renamed>' is not defined in module`.
            let exports: BTreeMap<String, String> = plan
                .bindings
                .iter()
                .filter(|(name, _)| {
                    binding_assignment.contains_key(&top_level_id(name, chunk_top_level_mark))
                })
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            if !exports.is_empty() {
                selected_exports_by_module[module_index] = Some(exports);
            }
        }
    });

    let mut entry_body = Vec::new();
    let import_insert_index = runtime_ast
        .module
        .body
        .iter()
        .take_while(|item| matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_))))
        .count();
    time_phase!(timings, "split_entry_body", {
        for (ordinal, item) in runtime_ast.module.body.iter().enumerate() {
            if !selected_ordinals.contains(&ordinal) {
                entry_body.push(item.clone());
                continue;
            }
            // Anonymous-statement members route the entire item to
            // the claiming module's body — no per-binding splitting.
            if let Some(module_index) = anonymous_ordinal_assignment.get(&ordinal).copied() {
                selected_by_module[module_index].push(item.clone());
                continue;
            }
            let mut remaining =
                remaining_item_after_selection(item, binding_assignment, &mut selected_by_module)?;
            entry_body.append(&mut remaining);
        }
        Ok::<_, anyhow::Error>(())
    })?;
    // Two passes: build entry imports in plan order (so the
    // first plan to claim a binding wins disambiguation), then
    // sort the resulting imports by `linker_order` so ECMA-262's
    // depth-first link traversal evaluates dependencies first.
    // Plan-order disambiguation + linker-order placement keeps
    // the import-collision contract while satisfying Lemma 2's
    // emit-side constraint. See DESIGN.md "Module dep graphs"
    // and "Lemma 2".
    let build_entry_imports_started = Instant::now();
    let mut entry_imports: Vec<(usize, ModuleItem)> = Vec::new();
    let mut occupied = collect_occupied_local_names(&entry_body);
    // `chunk_renames_plan` carries residual-side chunk_renames
    // (applied to entry body AND every moved body — moved bodies
    // reference residual bindings whose atoms are being changed)
    // plus every `binding_assignment` entry as a `MoveBinding`
    // (Phase 8a tracking). Sealed early; the resulting
    // `CheckedPlan` drives the plan-aware visitor in two places.
    let mut chunk_renames_plan = new_chunk_plan(&entry_body);
    {
        let mut sorted_moves: Vec<(&Id, &usize)> = binding_assignment.iter().collect();
        sorted_moves.sort_by(|a, b| (&a.0.0, a.0.1).cmp(&(&b.0.0, b.0.1)));
        for (id, module_index) in sorted_moves {
            chunk_renames_plan.submit(
                super::lowering_plan::LoweringOp::MoveBinding {
                    id: id.clone(),
                    to: ModuleId::logical(*module_index),
                    reason: "binding_assignment",
                },
                super::lowering_plan::SubmitPolicy::Fail,
            )?;
        }
    }
    let chunk_rename_map = submit_chunk_renames(
        &mut chunk_renames_plan,
        chunk_renames,
        binding_assignment,
        chunk_top_level_mark,
    )?;
    let chunk_renames_checked = chunk_renames_plan.seal()?;
    // `body_renames` retained for export-emit consumers
    // (`entry_exports_for_moved_bindings`, `auto_grown_residual_exports`,
    // `collect_entry_exports_by_original_local`) that still need a
    // `binding → exported_name` map keyed on atoms.
    let mut body_renames = chunk_rename_map;
    occupied.extend(body_renames.values().cloned());
    let nested_locals = collect_local_binding_names(&entry_body);
    occupied.extend(nested_locals.iter().cloned());
    // `entry_plan` accumulates entry-body-local renames
    // (cross-module import disambiguation per-module-plan).
    // Applies only to entry body — does NOT propagate to moved
    // bodies (a moved body's view of "import X from plan A" is
    // disambiguated independently in its own per-module plan).
    let mut entry_plan = new_chunk_plan(&entry_body);
    entry_plan.extend_occupied(
        Scope::Chunk,
        body_renames.values().map(|s| Atom::from(s.as_str())),
    );
    entry_plan.extend_occupied(
        Scope::Chunk,
        nested_locals.iter().map(|s| Atom::from(s.as_str())),
    );
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
            .filter(|(name, _)| {
                binding_assignment.contains_key(&top_level_id(name, chunk_top_level_mark))
            })
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        if live_bindings.is_empty() {
            continue;
        }
        let mut emit_renames = BTreeMap::<String, String>::new();
        let resolved = disambiguate_import_locals_via_plan(
            &mut entry_plan,
            &live_bindings,
            &mut occupied,
            &mut emit_renames,
            chunk_top_level_mark,
        )?;
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
            import_decl_for_plan(
                entry_file,
                &plan.target_file,
                &resolved,
                chunk_top_level_mark,
            ),
        ));
    }
    // Sort entry imports by ChunkFactorization::source_import_position, which
    // implements Lemma 2 (DESIGN.md "The realizability theorem"):
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
    let entry_checked = entry_plan.seal()?;
    {
        let rename_entry_body_started = Instant::now();
        // Apply chunk_renames (residual-side) AND entry-body
        // import-disambiguation renames in two plan-aware passes.
        // Each call pre-fills `export { local }` specifiers'
        // `exported` so the public name survives the rename,
        // then walks the body once renaming idents by
        // hygiene-preserving `Id`.
        apply_chunk_renames_to_items(&mut entry_body, &chunk_renames_checked);
        apply_chunk_renames_to_items(&mut entry_body, &entry_checked);
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
        // by construction (see DESIGN.md "Valid peels and atomic
        // modules", importability clause). The grow set excludes
        // names already in entry's source-level exports.
        let auto_grow = auto_grown_residual_exports(
            &selected_by_module,
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
    let mut source_import_cache =
        ArtifactSourceImportResolutionCache::new(artifact, artifact_indexes);

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
        metadata: FileMetadata {
            chunk_id: chunk_id.to_string(),
            chunk_file: entry_file.to_string(),
            role: FileRole::Entry,
            source_path: source_path.to_string(),
            generated_by_selected_module_lowering: false,
        },
    }];
    let mut file_records = vec![(entry_file.to_string(), FileRole::Entry)];
    let mut applied = Vec::new();

    // Filter chunk_renames down to entries the per-module emit path
    // should apply: bindings *not* claimed by any logical module.
    // (Residual-side chunk_renames apply to each moved body via
    // `apply_chunk_renames_to_items(&chunk_renames_checked)` —
    // see the per-iteration code below. The legacy
    // `cross_module_chunk_renames` BTreeMap + `IdentifierRenamer`
    // pass that used to do this work has been retired.)

    for (index, plan) in module_plans.iter().enumerate() {
        let mut body = std::mem::take(&mut selected_by_module[index]);
        // Per-moved-module plan: each emitted module body is a
        // separate ES-module scope, so its rename pipeline operates
        // on its own name pool. Sharing the chunk-wide plan would
        // surface false-positive cross-module collisions on bindings
        // that two different moved modules independently use.
        // Seeded BEFORE naturalize so the plan tracks pre-rename
        // atoms; naturalize submissions add their targets to the
        // plan's occupied set.
        let mut module_plan = super::lowering_plan::LoweringPlan::new(
            ModuleId::logical(0),
            Vec::new(),
            std::iter::once((
                Scope::Chunk,
                collect_local_binding_names(&body)
                    .iter()
                    .map(|s| Atom::from(s.as_str()))
                    .collect(),
            ))
            .collect(),
        );
        let local_renames = time_phase!(timings, "module.naturalize_body", {
            submit_naturalize_renames(&body, plan, &mut module_plan, chunk_top_level_mark)
        })?;
        let body_facts = time_phase!(timings, "module.collect_body_facts", {
            collect_module_body_facts(&body)
        });
        let ModuleReferenceNeeds {
            cross_module_imports_by_provider,
            residual_entry_imports,
            missing_residual_exports,
            runtime_reimports,
        } = time_phase!(timings, "module.plan_references", {
            plan_module_reference_needs(
                index,
                &body_facts,
                factorization,
                declaration_by_name,
                binding_assignment,
                &entry_exports_by_original_local,
                RuntimeImportLookup {
                    imports: runtime_import_facts,
                },
            )
        });
        let mut module_import_renames = BTreeMap::<String, String>::new();
        let mut module_import_locals = collect_local_binding_names(&body);
        let mut module_imports = time_phase!(timings, "module.build_cross_imports", {
            let mut ctx = super::imports_cross::RenameContext {
                plan: &mut module_plan,
                occupied: &mut module_import_locals,
                renames: &mut module_import_renames,
                chunk_top_level_mark,
            };
            cross_module_imports_for_plan(
                &plan.target_file,
                cross_module_imports_by_provider,
                factorization,
                &mut ctx,
            )
        })?;
        let mut residual_entry_imports = time_phase!(timings, "module.build_residual_imports", {
            let mut ctx = super::imports_cross::RenameContext {
                plan: &mut module_plan,
                occupied: &mut module_import_locals,
                renames: &mut module_import_renames,
                chunk_top_level_mark,
            };
            residual_entry_imports_for_moved_body(
                &plan.id,
                entry_file,
                &plan.target_file,
                residual_entry_imports,
                missing_residual_exports,
                &mut ctx,
            )
        })?;
        // Re-import any source-chunk import-specifier-bound locals that
        // moved code in `body` references but no top-level decl
        // satisfies (e.g. `const { decode } = gge;` where `gge` was an
        // ImportSpecifier in the source chunk's runtime body). Without
        // this, the moved code references a free variable and Node
        // throws `ReferenceError: gge is not defined` at runtime.
        let mut runtime_reimports = time_phase!(timings, "module.build_runtime_reimports", {
            source_chunk_imports_for_moved_body(
                &mut source_import_cache,
                chunk_top_level_mark,
                chunk_id,
                entry_file,
                &plan.target_file,
                runtime_reimports,
            )
        })?;
        module_imports.append(&mut residual_entry_imports);
        module_imports.append(&mut runtime_reimports);
        module_imports.append(&mut body);
        body = module_imports;
        // Apply the per-module plan (naturalize + cross-module +
        // residual-entry disambig) AND the chunk's residual
        // chunk_renames to the assembled body in one pass each.
        // The plan-aware visitor renames by `(Scope::Chunk, Id)`
        // — hygiene-preserving — so a function-local binding
        // with the same atom as a top-level one isn't touched,
        // and `export { local }` specifiers get their public
        // name preserved via the pre-fill of `exported`.
        let module_checked = module_plan.seal()?;
        time_phase!(timings, "module.apply_renames", {
            apply_plan_renames_and_naturalize(&mut body, &module_checked);
            apply_chunk_renames_to_items(&mut body, &chunk_renames_checked);
        });
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
                    .take_while(|item| {
                        matches!(item, ModuleItem::ModuleDecl(ModuleDecl::Import(_)))
                    })
                    .count();
                // `imported_from` on `BindingKind::Imported` is output-tree-
                // rooted absolute; `plan.target_file` is chunk-rooted. Lift
                // the destination to the same coordinate system before
                // computing the relative path.
                let dest_abs = join_module_path(&[chunk_id, &plan.target_file]);
                // Group reexports by rewritten source so multiple bindings
                // re-exported from the same import-from end up in a single
                // `import { ... } from "<src>"` statement, not one
                // statement per binding. First-occurrence order is
                // preserved for both source groups and bindings within
                // each group. All specifiers emitted here are Named, so
                // ESM's Namespace/Named mutual-exclusion rule doesn't
                // apply.
                let mut groups: Vec<(String, Vec<ImportSpecifier>)> =
                    Vec::with_capacity(reexports.len());
                let mut index_by_source: BTreeMap<String, usize> = BTreeMap::new();
                for reexport in reexports {
                    let src = relative_source(&dest_abs, &reexport.imported_from);
                    let specifier = imported_binding_named_specifier(
                        &reexport.local,
                        &reexport.imported_name,
                        chunk_top_level_mark,
                    );
                    let group_index = *index_by_source.entry(src.clone()).or_insert_with(|| {
                        groups.push((src.clone(), Vec::new()));
                        groups.len() - 1
                    });
                    groups[group_index].1.push(specifier);
                    import_member_exports
                        .insert(reexport.local.clone(), reexport.public_name.clone());
                }
                let mut reexport_imports = Vec::with_capacity(groups.len());
                for (src, specifiers) in groups {
                    reexport_imports.push(import_decl_module_item(specifiers, &src));
                }
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
        time_phase!(timings, "module.build_output_records", {
            // Materialize `plan.bindings` (a HashMap) in sorted order so
            // `binding_names`, `exported_names`, the header comment, and
            // the resolved `owner_ids` all share the same canonical
            // sequence regardless of hash seed.
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
                .map(|name| top_level_id(name, chunk_top_level_mark))
                .collect();
            let owner_ids = factorization
                .analysis
                .owner_report_ids_for_bindings(binding_ids.iter());
            let header = vec![
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
            ];
            files.push(JsFile {
                path: plan.target_file.clone(),
                body: JsFileBody::Ast(ParsedJsModule {
                    cm: runtime_ast.cm.clone(),
                    module: Module {
                        span: DUMMY_SP,
                        body,
                        shebang: None,
                    },
                    unresolved_mark: runtime_ast.unresolved_mark,
                    top_level_mark: runtime_ast.top_level_mark,
                }),
                header_lines: header,
                metadata: FileMetadata {
                    chunk_id: chunk_id.to_string(),
                    chunk_file: plan.target_file.clone(),
                    role: FileRole::Module,
                    source_path: source_path.to_string(),
                    generated_by_selected_module_lowering: true,
                },
            });
            file_records.push((plan.target_file.clone(), FileRole::Module));
            applied.push(SelectedModuleLowering {
                binding_names,
                chunk_id: chunk_id.to_string(),
                exported_names,
                file: entry_file.to_string(),
                id: plan.id.clone(),
                owner_ids,
                residual: !plan.explicit,
                target_file: plan.target_file.clone(),
                target_path: plan.target_path.clone(),
            });
        });
    }

    Ok(LoweredChunk {
        files,
        file_records,
        applied,
        timings,
    })
}
