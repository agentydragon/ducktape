//! Lower a single chunk: produce the per-chunk set of JS files (entry + extracted
//! logical modules) by running every module plan through the rename pipeline,
//! emitting cross-module imports, and naturalizing object shorthand. The chunk-
//! level orchestration that wraps this lives in mod.rs's
//! `materialize_logical_chunk`.

use super::util::{
    collect_local_binding_names, collect_occupied_local_names, disambiguate_import_locals,
    import_decl_for_plan, is_valid_js_identifier, preserve_export_specifier_names, relative_source,
    remaining_item_after_selection,
};
use super::*;
use crate::time_phase;

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
    pub(super) schedule: &'a ChunkFactorization,
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
        schedule,
        runtime_import_facts,
        chunk_renames,
        pre_existing_entry_exports,
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
        if binding_assignment.contains_key(&top_level_id(binding, chunk_top_level_mark)) {
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
        if binding_assignment.contains_key(&top_level_id(binding, chunk_top_level_mark)) {
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
            .filter(|(name, _)| {
                binding_assignment.contains_key(&top_level_id(name, chunk_top_level_mark))
            })
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
    // implements Lemma 2 (DESIGN.md "The realizability theorem"):
    // for acyclic imports graphs the order matches linker_order
    // (dependency-first source), but for cyclic-I shapes accepted
    // by the relaxed clause-3 rule the SCC members are reverse-
    // sorted so DFS unwinds the dependency first in post-order.
    // Stable sort preserves plan-order for ties.
    entry_imports.sort_by_key(|(idx, _)| {
        schedule
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
        let mut renamer = IdentifierRenamer {
            renames: &body_renames,
        };
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
            &entry_binding_renames,
        );
        if !auto_grow.is_empty() {
            entry_body.push(export_named_for_bindings(&auto_grow));
        }
        trim_dead_named_specifiers(&mut entry_body, &schedule.analysis.bindings);
    });
    let entry_exports_by_original_local = time_phase!(timings, "collect_entry_exports", {
        collect_entry_exports_by_original_local(
            &entry_body,
            &entry_binding_renames,
            chunk_top_level_mark,
        )
    });
    let imported_reexports_by_module = time_phase!(timings, "collect_imported_reexports", {
        collect_imported_reexports_by_module(schedule, module_plans.len())
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
    // Claimed bindings get their rename from the module plan
    // (handled via `disambiguate_import_locals` for cross-module
    // imports of the binding); the chunk_renames entry is dropped
    // for those. Mirrors the residual-side rule on body_renames
    // seeding above. The map is empty for chunks with no
    // chunk_renames; the per-module renamer is then a no-op.
    let cross_module_chunk_renames: BTreeMap<String, String> = chunk_renames
        .iter()
        .filter(|(binding, _)| {
            !binding_assignment.contains_key(&top_level_id(binding, chunk_top_level_mark))
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();

    for (index, plan) in module_plans.iter().enumerate() {
        let mut body = std::mem::take(&mut selected_by_module[index]);
        let local_renames = time_phase!(timings, "module.naturalize_body", {
            naturalize_module_body(&mut body, plan)
        });
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
                schedule,
                declaration_by_name,
                binding_assignment,
                &entry_exports_by_original_local,
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
                schedule,
                &mut module_import_locals,
                &mut module_import_renames,
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
            let mut renamer = IdentifierRenamer {
                renames: &module_import_renames,
            };
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
        let mut runtime_reimports = time_phase!(timings, "module.build_runtime_reimports", {
            source_chunk_imports_for_moved_body(
                &mut source_import_cache,
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
        // Apply chunk_renames to the assembled module body so that
        // import-specifier aliases and references in the moved code
        // both pick up the spec's rename. Without this, residual
        // entry says `getMobxGlobalState` but the peeled module's
        // `import { f as cx }` and `cx()` refs still say `cx`,
        // producing two disagreeing local aliases for the same
        // upstream binding.
        if !cross_module_chunk_renames.is_empty() {
            time_phase!(timings, "module.rename_chunk_renames", {
                let mut renamer = IdentifierRenamer {
                    renames: &cross_module_chunk_renames,
                };
                for item in body.iter_mut() {
                    item.visit_mut_with(&mut renamer);
                }
            });
        }
        time_phase!(timings, "module.rewrite_runtime_sources", {
            rewrite_runtime_sources_for_target(&mut body, chunk_id, entry_file, &plan.target_file);
        });
        // ImportSpecifier-bound members (`BindingKind::Imported` in
        // `schedule.analysis.bindings`): for each `Imported` binding whose
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
                    let specifier =
                        imported_binding_named_specifier(&reexport.local, &reexport.imported_name);
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
            let owner_ids = schedule
                .analysis
                .owner_report_ids_for_bindings(binding_names.iter().map(String::as_str));
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
