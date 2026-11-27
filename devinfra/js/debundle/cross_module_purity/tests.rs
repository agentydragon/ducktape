use super::callable_summaries::{
    CallableExportProof, CallableFactKey, CallableProjectionRules, CallableSummaryInputs,
    ResolvedCallableSummaries, resolve_imported_callable_summaries,
};
use super::export_primitives::{ExportValueSource, resolve_export_primitives};
use super::imported_purities::resolve_imported_purities;
use super::module_facts::{ModulePurityFacts, PreparationOptions};
use super::stable_imports::{StableImportRules, resolve_stable_imports};
use analysis::diagnostics::Diagnostics;
use analysis_model::PrimKinds;
use analysis_vocabulary::inputs::{
    CallableSummary, CallbackFactoryEvidence, PrimitiveExportCell, ProofDependencies,
    ReturnedRecordSummary,
};
use analysis_vocabulary::purity::Purity;
use anyhow::Result;
use inference::IterativeInference;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use value_flow::program_bindings::{
    ModuleBindings, ProgramBindings, ResolvedImport, SourceModuleKey,
};

use analysis_vocabulary::inputs::ValueInputRef;
use swc_common::{FileName, Span, sync::Lrc};
use swc_ecma_ast::{Expr, Module, NewExpr};
use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};
use swc_ecma_visit::{Visit, VisitWith};

fn parse(source: &str) -> Module {
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let fm = cm.new_source_file(FileName::Custom("m.js".into()).into(), source.to_string());
    let lexer = Lexer::new(
        Syntax::Es(Default::default()),
        Default::default(),
        StringInput::from(&*fm),
        None,
    );
    Parser::new_from(lexer).parse_module().unwrap()
}

#[test]
fn imported_primitive_value_closure_retains_all_export_cells() {
    let provider_cell = PrimitiveExportCell {
        module: "provider".to_string(),
        export: "limit".to_string(),
    };
    let bridge_cell = PrimitiveExportCell {
        module: "bridge".to_string(),
        export: "copied".to_string(),
    };
    let app_cell = PrimitiveExportCell {
        module: "app".to_string(),
        export: "forwarded".to_string(),
    };
    let sources = BTreeMap::from([
        (
            ("provider".to_string(), "limit".to_string()),
            ExportValueSource {
                kinds: PrimKinds::NUMBER,
                copies: Vec::new(),
                cell: Some(provider_cell.clone()),
            },
        ),
        (
            ("bridge".to_string(), "forwarded".to_string()),
            ExportValueSource {
                kinds: PrimKinds::NONE,
                copies: vec![ResolvedImport {
                    module: "provider".to_string(),
                    export: "limit".to_string(),
                    source_specifier: "provider".to_string(),
                }],
                cell: None,
            },
        ),
        (
            ("bridge".to_string(), "copied".to_string()),
            ExportValueSource {
                kinds: PrimKinds::NONE,
                copies: vec![ResolvedImport {
                    module: "provider".to_string(),
                    export: "limit".to_string(),
                    source_specifier: "provider".to_string(),
                }],
                cell: Some(bridge_cell.clone()),
            },
        ),
        (
            ("app".to_string(), "forwarded".to_string()),
            ExportValueSource {
                kinds: PrimKinds::NONE,
                copies: vec![ResolvedImport {
                    module: "bridge".to_string(),
                    export: "copied".to_string(),
                    source_specifier: "bridge".to_string(),
                }],
                cell: Some(app_cell.clone()),
            },
        ),
    ]);

    let bindings = ProgramBindings::new(
        [],
        sources
            .keys()
            .cloned()
            .chain(sources.values().flat_map(|source| {
                source
                    .copies
                    .iter()
                    .map(|copy| (copy.module.clone(), copy.export.clone()))
            })),
    );
    let resolved = resolve_export_primitives(&bindings, sources.clone());
    assert_eq!(
        resolved[&("bridge".to_string(), "forwarded".to_string())].kinds,
        PrimKinds::NUMBER
    );
    assert_eq!(
        resolved[&("bridge".to_string(), "forwarded".to_string())].source_cells,
        BTreeSet::from([provider_cell.clone()]),
    );
    assert_eq!(
        resolved[&("bridge".to_string(), "copied".to_string())].source_cells,
        BTreeSet::from([provider_cell.clone(), bridge_cell.clone()]),
    );
    assert_eq!(
        resolved[&("app".to_string(), "forwarded".to_string())].source_cells,
        BTreeSet::from([provider_cell, bridge_cell, app_cell]),
    );
}

fn facts<'a>(
    module: &'a Module,
    imports: &[(&str, &str, &str)],
    exports: &[(&str, &str)],
) -> ModulePurityFacts<'a> {
    ModulePurityFacts {
        body: &module.body,
        imports: imports
            .iter()
            .map(|(local, source, export)| {
                (
                    (*local).to_string(),
                    ResolvedImport {
                        module: (*source).to_string(),
                        export: (*export).to_string(),
                        source_specifier: (*source).to_string(),
                    },
                )
            })
            .collect(),
        exports: exports
            .iter()
            .map(|(name, local)| ((*name).to_string(), (*local).to_string()))
            .collect(),
        source: None,
        replacement_packages: BTreeMap::new(),
    }
}

fn imported_purities(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
) -> Result<BTreeMap<SourceModuleKey, BTreeMap<String, Arc<Purity>>>> {
    imported_purities_observed(modules, asserted, &Diagnostics::default())
}

fn imported_purities_observed(
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
    diagnostics: &Diagnostics,
) -> Result<BTreeMap<SourceModuleKey, BTreeMap<String, Arc<Purity>>>> {
    let bindings = binding_graph(modules);
    let stable = resolve_stable_imports(&bindings, modules, asserted);
    resolve_imported_purities(
        &stable,
        modules,
        asserted,
        &mut ResolvedCallableSummaries::default(),
        diagnostics,
    )
}

fn binding_graph(modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>) -> ProgramBindings {
    ProgramBindings::new(
        modules.iter().map(|(module, facts)| facts.bindings(module)),
        [],
    )
}

// Pure graph fixtures exercise solver work, not semantic body classification.
fn stable_graph(reexports: &[Option<usize>]) -> ProgramBindings {
    let imports = reexports
        .iter()
        .enumerate()
        .filter_map(|(node, target)| {
            Some((
                format!("link{node}"),
                ResolvedImport {
                    module: "graph".to_string(),
                    export: format!("node{}", (*target)?),
                    source_specifier: "./graph.js".to_string(),
                },
            ))
        })
        .collect();
    let exports = reexports
        .iter()
        .enumerate()
        .map(|(node, target)| {
            (
                format!("node{node}"),
                if target.is_some() {
                    format!("link{node}")
                } else {
                    format!("local{node}")
                },
            )
        })
        .collect();
    ProgramBindings::new(
        [ModuleBindings {
            module: "graph",
            imports: &imports,
            exports: &exports,
        }],
        [],
    )
}

#[test]
fn stable_import_inference_work_scales_with_shared_tail_and_unseeded_cycle_size() {
    for width in [8, 16, 32, 64] {
        let node_count = width * 2;
        let mut reexports = vec![None; node_count];
        reexports[..width].fill(Some(width));
        for (node, provider) in reexports
            .iter_mut()
            .enumerate()
            .take(node_count - 1)
            .skip(width)
        {
            *provider = Some(node + 1);
        }
        let bindings = stable_graph(&reexports);
        let rules = StableImportRules {
            bindings: &bindings,
            asserted_or_immutable: BTreeSet::from([bindings
                .export_id("graph", &format!("node{}", node_count - 1))
                .unwrap()]),
        };
        let solved = IterativeInference::new(rules).solve_values_resumable(bindings.exports());
        assert_eq!(solved.keys().count(), node_count, "width={width}");
        assert!(
            solved.evaluations() <= 2 * node_count,
            "width={width}: {} evaluations for {node_count} exports",
            solved.evaluations()
        );
    }
    for node_count in [8, 16, 32, 64, 128] {
        let bindings = stable_graph(
            &(0..node_count)
                .map(|node| Some((node + 1) % node_count))
                .collect::<Vec<_>>(),
        );
        let rules = StableImportRules {
            bindings: &bindings,
            asserted_or_immutable: BTreeSet::new(),
        };
        let solved = IterativeInference::new(rules).solve_values_resumable(bindings.exports());
        assert_eq!(solved.keys().count(), 0);
        assert_eq!(solved.evaluations(), node_count);
    }
}

fn function_purity_with_imports(
    source: &str,
    local: &str,
    imported: &BTreeMap<String, Arc<Purity>>,
) -> Purity {
    let module = parse(source);
    let facts = facts(&module, &[], &[("out", local)]);
    let prepared = facts.prepare(
        &analysis_vocabulary::inputs::AnalysisInputs::default(),
        None,
        PreparationOptions::default(),
    );
    facts
        .build_graph(&prepared, imported)
        .function_purity(local)
        .expect("function is indexed")
        .as_ref()
        .clone()
}

fn value_input_dependencies(provider: &str, input_index: usize) -> ProofDependencies {
    ProofDependencies {
        value_inputs: BTreeSet::from([ValueInputRef {
            defining_chunk: provider.to_string(),
            input_index,
        }]),
        ..Default::default()
    }
}

#[test]
fn callable_import_projection_preserves_typed_judgment_dependencies() {
    let app = parse("import { build } from 'provider';");
    let provider = parse("export function build() { return {}; }");
    let modules = BTreeMap::from([
        (
            "app".to_string(),
            facts(&app, &[("build", "provider", "build")], &[]),
        ),
        (
            "provider".to_string(),
            facts(&provider, &[], &[("build", "build")]),
        ),
    ]);
    let reported_dependencies = ValueInputRef {
        defining_chunk: "report-only.js".to_string(),
        input_index: 99,
    };
    let summary = CallableSummary::returned_record(ReturnedRecordSummary {
        defining_module: "provider".to_string(),
        export: "build".to_string(),
        local: "build".to_string(),
        source_location: None,
        members: Vec::new(),
        inference: analysis_vocabulary::provenance::InferenceUse {
            conclusion: analysis_vocabulary::provenance::InferenceRule::ReturnedRecord,
            derivation_rules: BTreeSet::from([
                analysis_vocabulary::provenance::InferenceRule::ReturnedRecord,
            ]),
            value_input_refs: BTreeSet::from([reported_dependencies]),
            descriptor_copy_author_refs: Default::default(),
            argument_input_refs: BTreeSet::new(),
            native_operation_input_refs: BTreeSet::new(),
            imported_primitive_array_spread_input_refs: BTreeSet::new(),
        },
        replacement_package: None,
    });
    let trusted_dependencies = value_input_dependencies("provider-values.js", 0);
    let mut export = CallableExportProof::default();
    export.add(summary.clone(), trusted_dependencies.clone());
    let bindings = binding_graph(&modules);
    let exports = BTreeMap::from([(bindings.export_id("provider", "build").unwrap(), export)]);
    let rules = CallableProjectionRules {
        bindings: &bindings,
        exports,
    };
    let producers = rules.producers();
    let solved = IterativeInference::new(rules).solve(producers);

    let import_key = CallableFactKey::Import(bindings.imports("app").next().unwrap().0);
    let judgment = solved
        .get(&import_key)
        .expect("provider summary projects onto the resolved importer");
    assert_eq!(judgment.value(), &summary);
    assert_eq!(
        judgment.dependencies(),
        &trusted_dependencies,
        "typed proof dependencies flow through the import; report references are projection-only"
    );
}

#[test]
fn progress_observes_preparation_live_before_the_operation_completes() {
    use progress::{Event, Phase, State};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::mpsc;
    use std::time::Duration;

    let app = parse("import { build } from 'provider'; build();");
    let provider = parse("export function build() { return {}; }");
    let modules = BTreeMap::from([
        (
            "app".to_string(),
            facts(&app, &[("build", "provider", "build")], &[]),
        ),
        (
            "provider".to_string(),
            facts(&provider, &[], &[("build", "build")]),
        ),
    ]);
    let (events, observed) = mpsc::channel();
    let (resume, paused) = mpsc::channel();
    let paused = std::sync::Mutex::new(paused);
    let complete = AtomicBool::new(false);
    let observer = |event: Event<'_>| {
        assert_eq!(event.phase, Phase::PrepareCrossModulePurity);
        assert_eq!(event.chunk, "provider");
        assert!(!complete.load(Ordering::SeqCst));
        events.send(event.state).unwrap();
        if event.state == State::Start {
            paused.lock().unwrap().recv().unwrap();
        }
    };
    let diagnostics = Diagnostics::with_progress(&observer);
    std::thread::scope(|scope| {
        let operation = scope.spawn(|| {
            let result = imported_purities_observed(&modules, &BTreeMap::new(), &diagnostics);
            complete.store(true, Ordering::SeqCst);
            result
        });
        let start = observed.recv_timeout(Duration::from_secs(10));
        // Always release the worker, including on a failed assertion.
        let still_running = !operation.is_finished();
        let _ = resume.send(());
        assert_eq!(start.unwrap(), State::Start);
        assert!(
            still_running,
            "observer must run before preparation finishes"
        );
        assert_eq!(
            observed.recv_timeout(Duration::from_secs(10)).unwrap(),
            State::End
        );
        assert!(operation.join().unwrap().unwrap()["app"]["build"].is_pure());
    });
    assert!(complete.load(Ordering::SeqCst));
}

#[test]
fn impurity_propagates_back_across_a_module_chain() {
    // c.base (impure) demotes b.wrap, which demotes a's `wrap` import.
    let a = parse("const W = wrap();");
    let b = parse("import { base } from \"c\";\nexport function wrap() { return base(); }");
    let c = parse("export function base() { globalSink(); }");
    let modules = BTreeMap::from([
        ("a".to_string(), facts(&a, &[("wrap", "b", "wrap")], &[])),
        (
            "b".to_string(),
            facts(&b, &[("base", "c", "base")], &[("wrap", "wrap")]),
        ),
        ("c".to_string(), facts(&c, &[], &[("base", "base")])),
    ]);
    let resolved = imported_purities(&modules, &BTreeMap::new()).unwrap();
    assert!(!resolved["a"]["wrap"].is_pure());
}

#[test]
fn mutually_recursive_imported_functions_close_without_optimistic_seeds() {
    let a = parse("import { right } from 'b'; export function left() { return right(); }");
    let b = parse("import { left } from 'a'; export function right() { return left(); }");
    let modules = BTreeMap::from([
        (
            "a".to_string(),
            facts(&a, &[("right", "b", "right")], &[("left", "left")]),
        ),
        (
            "b".to_string(),
            facts(&b, &[("left", "a", "left")], &[("right", "right")]),
        ),
    ]);

    let resolved = imported_purities(&modules, &BTreeMap::new()).unwrap();

    assert!(resolved["a"]["right"].is_pure());
    assert!(resolved["b"]["left"].is_pure());
}

#[test]
fn impurity_in_mutually_recursive_imports_has_finite_cause_chains() {
    let a = parse("import { right } from 'b'; export function left() { return right(); }");
    let b = parse("import { left } from 'a'; export function right() { left(); globalSink(); }");
    let modules = BTreeMap::from([
        (
            "a".to_string(),
            facts(&a, &[("right", "b", "right")], &[("left", "left")]),
        ),
        (
            "b".to_string(),
            facts(&b, &[("left", "a", "left")], &[("right", "right")]),
        ),
    ]);

    let resolved = imported_purities(&modules, &BTreeMap::new()).unwrap();

    let right = &resolved["a"]["right"];
    let left = &resolved["b"]["left"];
    assert!(!right.is_pure());
    assert!(!left.is_pure());
    assert_finite_causes(right);
    assert_finite_causes(left);
}

fn assert_finite_causes(root: &Arc<Purity>) {
    fn visit(purity: &Arc<Purity>, active: &mut BTreeSet<usize>, completed: &mut BTreeSet<usize>) {
        let identity = Arc::as_ptr(purity) as usize;
        assert!(
            !active.contains(&identity),
            "purity cause graph contains a cycle"
        );
        if !completed.insert(identity) {
            return;
        }
        active.insert(identity);
        if let Purity::NotPure { reasons } = purity.as_ref() {
            for reason in reasons {
                if let Some(cause) = &reason.cause {
                    visit(cause, active, completed);
                }
            }
        }
        active.remove(&identity);
    }

    visit(root, &mut BTreeSet::new(), &mut BTreeSet::new());
}

#[test]
fn import_of_an_unanalyzed_module_stays_unknown() {
    // `vendor` is not in the module set (external/opaque) → no entry.
    let app = parse("const C = ext(x);");
    let modules = BTreeMap::from([(
        "app".to_string(),
        facts(&app, &[("ext", "vendor", "ext")], &[]),
    )]);
    let resolved = imported_purities(&modules, &BTreeMap::new()).unwrap();
    assert!(!resolved["app"].contains_key("ext"));
}

#[test]
fn import_of_a_non_function_export_stays_unknown() {
    // A data export is not callable → no purity entry, stays unknown_call.
    let app = parse("const C = table(x);");
    let lib = parse("export const table = 42;");
    let modules = BTreeMap::from([
        (
            "app".to_string(),
            facts(&app, &[("table", "lib", "table")], &[]),
        ),
        ("lib".to_string(), facts(&lib, &[], &[("table", "table")])),
    ]);
    let resolved = imported_purities(&modules, &BTreeMap::new()).unwrap();
    assert!(!resolved["app"].contains_key("table"));
}

#[test]
fn cross_module_graph_preparation_gates_mutated_set_iterable_proof() {
    let module =
        parse("const values = [1]; Set.prototype.add = function add(value) {}; new Set(values);");
    let prepared = facts(&module, &[], &[]).prepare(
        &analysis_vocabulary::inputs::AnalysisInputs::default(),
        None,
        PreparationOptions::default(),
    );

    struct SetConstructors(Vec<Span>);
    impl Visit for SetConstructors {
        fn visit_new_expr(&mut self, node: &NewExpr) {
            if matches!(node.callee.unwrap_parens(), Expr::Ident(ident) if ident.sym.as_ref() == "Set")
            {
                self.0.push(node.span);
            }
            node.visit_children_with(self);
        }
    }
    let mut constructors = SetConstructors(Vec::new());
    module.visit_with(&mut constructors);
    assert_eq!(constructors.0.len(), 1, "fixture needs one Set constructor");
    assert!(
        prepared
            .set_iterable_proof
            .dependencies_for_constructor(constructors.0[0])
            .is_none(),
        "cross-module purity preparation must honor the canonical intrinsic census"
    );
}

#[test]
fn asserted_pure_export_overrides_inferred_impurity() {
    // `observer` is genuinely impure to the classifier (writes a global),
    // so inference demotes it. An author assertion pins it Pure, and the
    // verdict propagates to the importing chunk.
    let app = parse("const C = observer(x);");
    let lib = parse("export function observer(c) { globalThis.__warned = 1; return c; }");
    let modules = BTreeMap::from([
        (
            "app".to_string(),
            facts(&app, &[("observer", "lib", "observer")], &[]),
        ),
        (
            "lib".to_string(),
            facts(&lib, &[], &[("observer", "observer")]),
        ),
    ]);
    // Without the assertion: impure.
    assert!(!imported_purities(&modules, &BTreeMap::new()).unwrap()["app"]["observer"].is_pure());
    // With the assertion on the defining chunk: pure, at every importer.
    let asserted = BTreeMap::from([("lib".to_string(), BTreeSet::from(["observer".to_string()]))]);
    let resolved = imported_purities(&modules, &asserted).unwrap();
    assert!(resolved["app"]["observer"].is_pure());
    assert_eq!(
        resolved["app"]["observer"].dependencies(),
        Some(&ProofDependencies::default()),
        "the separate pure_exports axiom must not acquire inferred captured-value dependencies"
    );
}

#[test]
fn imported_pure_dependencies_are_retained_by_local_wrappers() {
    let dependencies = value_input_dependencies("static/provider.js", 1);
    let imported = BTreeMap::from([(
        "upstream".to_string(),
        Arc::new(Purity::pure().with_dependencies(&dependencies)),
    )]);
    let wrapper = function_purity_with_imports(
        "export function forward() { return upstream(); }",
        "forward",
        &imported,
    );
    assert_eq!(wrapper.dependencies(), Some(&dependencies));
}

#[test]
fn asserted_pure_export_applies_to_a_non_function_callable() {
    // The export is a re-exported interop value the classifier does not see
    // as a chunk-top function, so inference yields no verdict. The
    // assertion still admits it (author trust), reaching the importer.
    let app = parse("const C = forwardRef(x);");
    let lib = parse("const inner = makeRef();\nexport { inner as forwardRef };");
    let modules = BTreeMap::from([
        (
            "app".to_string(),
            facts(&app, &[("forwardRef", "lib", "forwardRef")], &[]),
        ),
        (
            "lib".to_string(),
            facts(&lib, &[], &[("forwardRef", "inner")]),
        ),
    ]);
    assert!(
        !imported_purities(&modules, &BTreeMap::new()).unwrap()["app"].contains_key("forwardRef")
    );
    let asserted = BTreeMap::from([(
        "lib".to_string(),
        BTreeSet::from(["forwardRef".to_string()]),
    )]);
    assert!(imported_purities(&modules, &asserted).unwrap()["app"]["forwardRef"].is_pure());
}

#[test]
fn callback_factory_summaries_keep_source_and_assertion_provenance() {
    let app = parse(
        "import { wrap as localWrap, audited as localAudited } from 'lib';\n\
         import { wrap as missingWrap } from 'vendor';",
    );
    let lib = parse(
        "export function wrap(callback) { return () => callback(); }\n\
         export const audited = makeOpaque();",
    );
    let modules = BTreeMap::from([
        (
            "app".to_string(),
            facts(
                &app,
                &[
                    ("localWrap", "lib", "wrap"),
                    ("localAudited", "lib", "audited"),
                    ("missingWrap", "vendor", "wrap"),
                ],
                &[],
            ),
        ),
        (
            "lib".to_string(),
            facts(&lib, &[], &[("wrap", "wrap"), ("audited", "audited")]),
        ),
    ]);
    let asserted = BTreeMap::from([
        ("lib".to_string(), BTreeSet::from(["audited".to_string()])),
        ("vendor".to_string(), BTreeSet::from(["wrap".to_string()])),
    ]);
    let resolved = resolve_imported_callable_summaries(
        &binding_graph(&modules),
        &modules,
        &asserted,
        &BTreeMap::new(),
        CallableSummaryInputs {
            closed_factory_imports: &BTreeMap::new(),
            namespace_privacy_members: &BTreeMap::new(),
            callback_factory_members: &BTreeMap::new(),
            requested_original_provider_exports: &BTreeSet::new(),
            selected_value_inputs: &BTreeMap::new(),
            selected_native_operation_inputs: &BTreeMap::new(),
            selected_imported_primitive_array_spread_inputs: &BTreeMap::new(),
            argument_inputs: &BTreeMap::new(),
            predefined_globals: spec::PredefinedGlobals::Unknown,
            no_accessor_install_callables: &BTreeMap::new(),
            intrinsics_never_written: BTreeSet::new(),
        },
        &Diagnostics::default(),
    )
    .unwrap()
    .imported_callables;
    assert_eq!(
        resolved["app"]["localWrap"].callback_factory_evidence(),
        &BTreeSet::from([CallbackFactoryEvidence::InferredImmutableWrapper {
            defining_module: "lib".to_string(),
            export: "wrap".to_string(),
            local: "wrap".to_string(),
            source_location: None,
        }]),
    );
    assert_eq!(
        resolved["app"]["localAudited"].callback_factory_evidence(),
        &BTreeSet::from([CallbackFactoryEvidence::AuthorAssertion {
            defining_module: "lib".to_string(),
            export: "audited".to_string(),
        }]),
    );
    assert!(!resolved["app"].contains_key("missingWrap"));
}
