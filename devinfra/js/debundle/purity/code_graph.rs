use super::*;
use crate::facts::abstract_domain::heap;
use crate::facts::abstract_domain::primitive::{BindingPrims, collect_binding_prims};
use crate::facts::{
    DynamicScopeFact, LocalClassConstructionProof, LocalClassConstructionShapes, SetIterableProof,
    SetIterableShapes, StructuralStatementFacts,
};
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

/// Chunk-wide code graph: indexes top-level bindings and answers
/// queries the classifier needs that go beyond per-expression
/// inspection. Currently exposes function-body purity for
/// chunk-local Ident callees (used by `classify_call_purity` to
/// short-circuit `Pure` callees). Designed to grow into a fuller
/// binding-shape model — import provenance, var-init purity,
/// class shape, etc. — as further analyses land. New binding
/// kinds add new `ChunkBinding` variants and matching query
/// methods; the iteration in `ChunkCodeGraph::build` extends to
/// them naturally.
#[derive(Debug, Default, Clone)]
pub struct ChunkCodeGraph {
    pub(crate) bindings: BTreeMap<String, ChunkBinding>,
    plain_data_dependencies: BTreeMap<String, crate::analysis_inputs::ProofDependencies>,
    plain_data_inference_uses: BTreeMap<String, BTreeSet<crate::reports::schema::InferenceUse>>,
    fresh_record_proofs: BTreeMap<u32, FreshRecordProof>,
    pure_new_constructors: BTreeSet<String>,
    /// Per-binding declared-pure member-call set. Looked up by the
    /// classifier when it sees `<recv>.<prop>(args)` — if `recv` is a
    /// non-shadowed key here and `<prop>` is in the set, the call is
    /// admitted as pure with args evaluated normally. Author-trust
    /// contract; see <docs/purity_soundness.md> "Declared purity".
    declared_pure_members: BTreeMap<String, BTreeSet<String>>,
    /// Primitive types of chunk-top consts and stable top-level numeric vars,
    /// read by operator classification. See `collect_binding_prims`.
    pub(crate) binding_prims: BindingPrims,
    /// Projection of `binding_prims`: bindings whose values carry no user
    /// accessors and are never Symbol, so coercing a reference fires no user
    /// code. Init effects remain classified separately.
    pub(crate) primitive_bindings: BTreeSet<String>,
    /// Projection of `binding_prims`: Symbol-valued bindings are safe only for
    /// ToPropertyKey; ToString(Symbol) throws.
    pub(crate) symbol_const_bindings: BTreeSet<String>,
    /// Chunk-top `const X = [..]` bindings that remain static ordinary
    /// arrays under the same no-escape scan as `PlainData`, and are
    /// never used as the receiver of unknown/mutating array methods.
    /// At-init fallback uses this to avoid treating `X.map(cb)` /
    /// `X.forEach(cb)` as if those built-ins might invoke functions
    /// held inside `X`; the callback body is still modeled separately.
    plain_array_bindings: BTreeSet<String>,
    /// Candidate own primitive fields of single-init TS enum objects.
    ts_enum_named_data_candidates: BTreeMap<Id, TsEnumNamedDataCandidate>,
    /// Primitive enum fields are safe computed property keys only when the
    /// enum binding is not rebound outside its recognized initializer.
    stable_ts_enum_primitive_members: BTreeMap<String, BTreeSet<String>>,
    /// Exact Set-constructor spans with a structurally checked fresh-array
    /// iterable. The same proof is consumed by synchronous effect analysis.
    set_iterable_proof: SetIterableProof,
    local_class_construction_proof: LocalClassConstructionProof,
    /// Purity verdict for chunk-top bindings that are imports of a
    /// function from another module, keyed by local binding name.
    /// Populated by the program-level cross-module purity oracle;
    /// empty in the strictly per-chunk path, so an imported callee with
    /// no entry stays `unknown_call` as before. A body-local binding
    /// that shadows the import is not resolved through this map.
    imported_purities: BTreeMap<String, Arc<Purity>>,
    /// Bindings the author asserts are *fluent-trusted* roots
    /// (`chunk_export_purity.<chunk>.fluent_exports`, projected onto
    /// this chunk's local import bindings): every value reachable from
    /// the root through static member reads and calls is itself
    /// trusted — member reads on it are pure, calls of it are pure
    /// (arguments still classified normally), and the result carries
    /// the same trust. This is what admits builder/fluent APIs whose
    /// chain receivers are call *results*, not bindings — e.g. zod's
    /// `k.object({...}).optional().describe(...)` — which no
    /// binding-keyed surface (`declared_pure_members`,
    /// `imported_purities`) can ever reach. Seeded from
    /// `AnalysisInputs::fluent_bindings` and closed over chunk-top
    /// `const X = <fluent chain>` declarations
    /// (`collect_fluent_const_bindings`). Author-trust contract: see
    /// docs/design.md A9.
    fluent_bindings: BTreeSet<String>,
    /// During bottom-up transfer extraction, inferred calls are recorded as
    /// obligations instead of consulting an optimistic Pure placeholder.
    purity_call_targets: BTreeMap<String, PurityFunctionKey>,
    purity_call_obligations: Option<Rc<RefCell<Vec<PurityCallObligation>>>>,
}

/// Prepared static facts consumed while extracting canonical purity bodies.
/// Grouping these inputs keeps local and program-wide extraction on the same
/// gates for native, captured, and shape-based facts.
pub(crate) struct PurityShapeInputs<'a> {
    pub(crate) shadowed: &'a BTreeSet<&'static str>,
    pub(crate) declared_pure_new: &'a BTreeSet<String>,
    pub(crate) declared_pure_members: &'a BTreeMap<String, BTreeSet<String>>,
    pub(crate) imported_purities: &'a BTreeMap<String, Arc<Purity>>,
    pub(crate) fluent_bindings: &'a BTreeSet<String>,
    pub(crate) set_iterable_proof: &'a SetIterableProof,
    pub(crate) local_class_construction_proof: &'a LocalClassConstructionProof,
    pub(crate) plain_data_bindings: &'a [String],
    pub(crate) plain_data_dependencies:
        &'a BTreeMap<String, crate::analysis_inputs::ProofDependencies>,
    pub(crate) plain_data_inference_uses:
        &'a BTreeMap<String, BTreeSet<crate::reports::schema::InferenceUse>>,
    pub(crate) plain_array_bindings: &'a BTreeSet<String>,
    pub(crate) stable_var_number_candidates: &'a BTreeSet<Id>,
    pub(crate) structural_facts: Option<&'a [StructuralStatementFacts]>,
}

/// Source-module identity, policy, and inferred imported function targets for
/// one batch of body transfers.
pub(crate) struct PurityCallContext<'a> {
    pub(crate) module: &'a str,
    pub(crate) mode: PurityMode,
    pub(crate) declared_pure: &'a BTreeSet<String>,
    pub(crate) imported_targets: &'a BTreeMap<String, PurityFunctionKey>,
}

#[derive(Debug, Clone)]
pub(crate) enum ChunkBinding {
    /// Chunk-top function declaration or `const f = function/arrow`.
    /// `purity` is the worst purity reachable from the body, computed
    /// by fixed-point iteration over all chunk-top functions.
    Function { purity: Arc<Purity> },
    /// Chunk-top binding whose value is provably a plain object/array
    /// — `const X = <plain literal>`, or `let`/`var` whose every
    /// re-bind is also a plain literal, or the TS-enum-IIFE shape
    /// (see `is_ts_enum_iife_init_for_binding`) — whose allocations stay
    /// private and unwritten (`facts/abstract_domain/heap.rs`), so
    /// property reads `X.k` / `X[k]` are pure.
    /// Full soundness write-up: <docs/purity_soundness.md> §
    /// "ChunkBinding::PlainData".
    PlainData,
}

impl ChunkCodeGraph {
    pub(crate) fn fresh_record_proof(&self, key: u32) -> Option<&FreshRecordProof> {
        self.fresh_record_proofs.get(&key)
    }

    /// Build the graph for `body`. Canonical body classification produces
    /// immutable transfer facts and exact inferred call obligations; the
    /// effects worklist closes recursion before projecting final verdicts.
    pub(crate) fn build(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
    ) -> Self {
        Self::build_with_declared_pure_new(body, shadowed, declared_pure, &BTreeSet::new())
    }

    pub(crate) fn build_with_declared_pure_new(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
        declared_pure_new: &BTreeSet<String>,
    ) -> Self {
        Self::build_full(
            body,
            shadowed,
            declared_pure,
            declared_pure_new,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &BTreeSet::new(),
        )
    }

    pub(crate) fn build_full(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
        declared_pure_new: &BTreeSet<String>,
        declared_pure_members: &BTreeMap<String, BTreeSet<String>>,
        imported_purities: &BTreeMap<String, Arc<Purity>>,
        fluent_bindings: &BTreeSet<String>,
    ) -> Self {
        Self::build_full_with_locations(
            body,
            shadowed,
            declared_pure,
            declared_pure_new,
            declared_pure_members,
            imported_purities,
            fluent_bindings,
            None,
        )
    }

    #[allow(clippy::too_many_arguments)] // The optional resolver extends the established build_full inputs.
    pub(crate) fn build_full_with_locations(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
        declared_pure_new: &BTreeSet<String>,
        declared_pure_members: &BTreeMap<String, BTreeSet<String>>,
        imported_purities: &BTreeMap<String, Arc<Purity>>,
        fluent_bindings: &BTreeSet<String>,
        locate: Option<&mut dyn FnMut(Span) -> Option<SourceLocation>>,
    ) -> Self {
        Self::build_full_with_locations_and_stable_vars(
            body,
            shadowed,
            declared_pure,
            declared_pure_new,
            declared_pure_members,
            imported_purities,
            fluent_bindings,
            &BTreeSet::new(),
            locate,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn build_full_with_locations_and_stable_vars(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
        declared_pure_new: &BTreeSet<String>,
        declared_pure_members: &BTreeMap<String, BTreeSet<String>>,
        imported_purities: &BTreeMap<String, Arc<Purity>>,
        fluent_bindings: &BTreeSet<String>,
        stable_var_number_candidates: &BTreeSet<Id>,
        locate: Option<&mut dyn FnMut(Span) -> Option<SourceLocation>>,
    ) -> Self {
        let value_flow = crate::facts::ChunkValueFlow::analyze_with_candidates(
            body,
            &heap::tracked_allocation_roots(body),
        );
        let plain_data_bindings = heap::ChunkHeap::analyze(body, shadowed, &value_flow, None)
            .plain_data_bindings()
            .bindings;
        let plain_array_bindings = heap::plain_array_bindings(body, shadowed);
        let dynamic_scope = DynamicScopeFact::analyze(body);
        let local_class_construction_proof =
            LocalClassConstructionShapes::analyze(body, shadowed, &value_flow, dynamic_scope);
        let set_iterable_proof = SetIterableShapes::analyze(
            body,
            shadowed,
            &value_flow,
            &plain_data_bindings,
            dynamic_scope,
        );
        let prepared_sync_index =
            crate::facts::sync_effects::execution_index::interpreter::prepare_synchronous_call_index(
                body,
                &value_flow,
                &crate::analysis_inputs::AnalysisInputs::default(),
                &plain_array_bindings,
                &local_class_construction_proof,
                set_iterable_proof,
                shadowed,
            );
        let local_class_construction_proof =
            prepared_sync_index.local_class_construction_proof().clone();
        let set_iterable_proof = prepared_sync_index.set_iterable_proof().clone();
        Self::build_full_with_prepared_plain_data(
            body,
            shadowed,
            declared_pure,
            declared_pure_new,
            declared_pure_members,
            imported_purities,
            fluent_bindings,
            &set_iterable_proof,
            &local_class_construction_proof,
            &plain_data_bindings,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &plain_array_bindings,
            stable_var_number_candidates,
            None,
            locate,
        )
    }

    /// Reuse prepared chunk facts, including stable numeric-var candidates from
    /// the canonical structural analysis. Callers without those candidates pass
    /// an empty set and remain conservative.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn build_full_with_prepared_plain_data(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
        declared_pure_new: &BTreeSet<String>,
        declared_pure_members: &BTreeMap<String, BTreeSet<String>>,
        imported_purities: &BTreeMap<String, Arc<Purity>>,
        fluent_bindings: &BTreeSet<String>,
        set_iterable_proof: &SetIterableProof,
        local_class_construction_proof: &LocalClassConstructionProof,
        plain_data_bindings: &[String],
        plain_data_dependencies: &BTreeMap<String, crate::analysis_inputs::ProofDependencies>,
        plain_data_inference_uses: &BTreeMap<
            String,
            BTreeSet<crate::reports::schema::InferenceUse>,
        >,
        plain_array_bindings: &BTreeSet<String>,
        stable_var_number_candidates: &BTreeSet<Id>,
        structural_facts: Option<&[StructuralStatementFacts]>,
        mut locate: Option<&mut dyn FnMut(Span) -> Option<SourceLocation>>,
    ) -> Self {
        let functions = collect_chunk_functions(body);
        let shape_inputs = PurityShapeInputs {
            shadowed,
            declared_pure_new,
            declared_pure_members,
            imported_purities,
            fluent_bindings,
            set_iterable_proof,
            local_class_construction_proof,
            plain_data_bindings,
            plain_data_dependencies,
            plain_data_inference_uses,
            plain_array_bindings,
            stable_var_number_candidates,
            structural_facts,
        };
        let local_targets = BTreeMap::new();
        let call_context = PurityCallContext {
            module: "",
            mode: PurityMode::Local,
            declared_pure,
            imported_targets: &local_targets,
        };
        let mut graph =
            Self::build_effect_collection_graph(body, &functions, &shape_inputs, &call_context);
        let mut bodies = BTreeMap::new();
        for function in &functions {
            let key = PurityFunctionKey {
                module: String::new(),
                binding: function.name.clone(),
                source_key: function.key,
                mode: PurityMode::Local,
            };
            bodies.insert(
                key,
                classify_function_body_transfer(
                    function,
                    shadowed,
                    declared_pure,
                    &graph,
                    &mut locate,
                ),
            );
        }
        let purities = PurityEffectRules::new(bodies).solve_to_purities();
        for function in functions {
            let key = PurityFunctionKey {
                module: String::new(),
                binding: function.name.clone(),
                source_key: function.key,
                mode: PurityMode::Local,
            };
            let purity = purities
                .get(&key)
                .expect("each function transfer has a completed effect fact")
                .clone();
            graph
                .bindings
                .insert(function.name, ChunkBinding::Function { purity });
        }
        // The returned graph is used by later expression classification,
        // which must consume the finalized local purity verdicts rather than
        // record new transfer obligations.
        graph.purity_call_targets.clear();
        graph.purity_call_obligations = None;
        graph
    }

    /// Stable identities for every chunk-top function in source order. Export
    /// analysis uses these identities to flatten module boundaries into one
    /// effects closure.
    pub(crate) fn inferred_function_keys(
        body: &[TopLevelItemView<'_>],
        module: &str,
        mode: PurityMode,
    ) -> BTreeMap<String, PurityFunctionKey> {
        collect_chunk_functions(body)
            .into_iter()
            .map(|function| {
                (
                    function.name.clone(),
                    PurityFunctionKey {
                        module: module.to_string(),
                        binding: function.name,
                        source_key: function.key,
                        mode,
                    },
                )
            })
            .collect()
    }

    /// Extract canonical body classifications while replacing only inferred
    /// local/imported function lookups with exact call obligations. All other
    /// precedence, shadowing, argument, native, and captured-input rules stay
    /// in the existing classifier.
    pub(crate) fn effect_transfers(
        body: &[TopLevelItemView<'_>],
        shape_inputs: &PurityShapeInputs<'_>,
        call_context: &PurityCallContext<'_>,
        mut locate: Option<&mut dyn FnMut(Span) -> Option<SourceLocation>>,
    ) -> BTreeMap<PurityFunctionKey, PurityBodyTransfer> {
        let functions = collect_chunk_functions(body);
        let graph =
            Self::build_effect_collection_graph(body, &functions, shape_inputs, call_context);
        let mut transfers = BTreeMap::new();
        for function in functions {
            let key = PurityFunctionKey {
                module: call_context.module.to_string(),
                binding: function.name.clone(),
                source_key: function.key,
                mode: call_context.mode,
            };
            let transfer = classify_function_body_transfer(
                &function,
                shape_inputs.shadowed,
                call_context.declared_pure,
                &graph,
                &mut locate,
            );
            transfers.insert(key, transfer);
        }
        transfers
    }

    fn build_effect_collection_graph(
        body: &[TopLevelItemView<'_>],
        functions: &[ChunkFunction<'_>],
        shape_inputs: &PurityShapeInputs<'_>,
        call_context: &PurityCallContext<'_>,
    ) -> Self {
        // Both compatibility analysis and the program-wide closure use this
        // exact static graph, so native/captured facts and all shape gates
        // cannot drift between the two paths.
        let function_names: BTreeSet<_> = functions
            .iter()
            .map(|function| function.name.clone())
            .collect();
        let bindings: BTreeMap<String, ChunkBinding> = shape_inputs
            .plain_data_bindings
            .iter()
            .filter(|name| !function_names.contains(*name))
            .map(|name| (name.clone(), ChunkBinding::PlainData))
            .collect();
        let plain_data_dependencies = shape_inputs.plain_data_dependencies.clone();
        let plain_data_inference_uses = shape_inputs.plain_data_inference_uses.clone();
        let ts_enum_named_data_candidates: BTreeMap<Id, TsEnumNamedDataCandidate> =
            collect_ts_enum_named_data_candidates(body, shape_inputs.plain_data_bindings)
                .into_iter()
                .filter(|(binding, _)| {
                    matches!(
                        bindings.get(binding.0.as_ref()),
                        Some(ChunkBinding::PlainData)
                    )
                })
                .collect();
        let binding_prims = collect_binding_prims(
            body,
            shape_inputs.shadowed,
            shape_inputs.stable_var_number_candidates,
        );
        let stable_ts_enum_primitive_members = shape_inputs
            .structural_facts
            .map(|facts| {
                ts_enum_named_data_candidates
                    .iter()
                    .filter(|(binding, candidate)| {
                        !facts.iter().any(|fact| {
                            fact.ordinal != candidate.initializer_source_unit
                                && (fact.rebinds.eager.contains(*binding)
                                    || fact.rebinds.lazy.contains(*binding))
                        })
                    })
                    .map(|(binding, candidate)| {
                        (binding.0.to_string(), candidate.own_primitive_keys.clone())
                    })
                    .collect()
            })
            .unwrap_or_default();
        let mut purity_call_targets = call_context.imported_targets.clone();
        purity_call_targets.extend(Self::inferred_function_keys(
            body,
            call_context.module,
            call_context.mode,
        ));
        Self {
            bindings,
            fresh_record_proofs: functions
                .iter()
                .filter_map(|function| {
                    let body = function.block_body?;
                    Some((function.key, fresh_record_proof(&function.params, body)?))
                })
                .collect(),
            pure_new_constructors: shape_inputs.declared_pure_new.clone(),
            declared_pure_members: shape_inputs.declared_pure_members.clone(),
            primitive_bindings: binding_prims.non_symbol_bindings(),
            symbol_const_bindings: binding_prims.symbol_bindings(),
            binding_prims,
            plain_array_bindings: shape_inputs.plain_array_bindings.clone(),
            ts_enum_named_data_candidates,
            plain_data_dependencies,
            plain_data_inference_uses,
            stable_ts_enum_primitive_members,
            set_iterable_proof: shape_inputs.set_iterable_proof.clone(),
            local_class_construction_proof: shape_inputs.local_class_construction_proof.clone(),
            imported_purities: shape_inputs.imported_purities.clone(),
            fluent_bindings: collect_fluent_const_bindings(body, shape_inputs.fluent_bindings),
            purity_call_targets,
            purity_call_obligations: Some(Rc::new(RefCell::new(Vec::new()))),
        }
    }

    /// Purity of the chunk-local function bound to `name`, if any.
    /// Returns `None` for non-function bindings (imports, vars,
    /// classes) and for names not bound at chunk top.
    pub(crate) fn function_purity(&self, name: &str) -> Option<&Arc<Purity>> {
        match self.bindings.get(name)? {
            ChunkBinding::Function { purity } => Some(purity),
            ChunkBinding::PlainData => None,
        }
    }

    /// A stable recognized enum's named field is known to hold a primitive
    /// literal. This proves ToPropertyKey cannot invoke user code while
    /// retaining the ordinary member-read purity and binding-order checks.
    pub(crate) fn is_stable_ts_enum_primitive_member(
        &self,
        expression: &Expr,
        local_shadowed: &BTreeSet<String>,
    ) -> bool {
        let Expr::Member(member) = expression.unwrap_parens() else {
            return false;
        };
        let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
            return false;
        };
        let MemberProp::Ident(property) = &member.prop else {
            return false;
        };
        let receiver_name = receiver.sym.as_ref();
        !local_shadowed.contains(receiver_name)
            && self
                .stable_ts_enum_primitive_members
                .get(receiver_name)
                .is_some_and(|members| members.contains(property.sym.as_ref()))
    }

    /// Return and record the inferred target of a direct identifier call when
    /// this graph is collecting body transfers. Returning `None` leaves the
    /// established classifier path unchanged.
    pub(crate) fn record_purity_call_obligation(
        &self,
        name: &str,
        span: Span,
    ) -> Option<PurityFunctionKey> {
        let target = self.purity_call_targets.get(name)?.clone();
        self.purity_call_obligations
            .as_ref()?
            .borrow_mut()
            .push(PurityCallObligation {
                callee: target.clone(),
                name: name.to_string(),
                span,
                source_location: None,
            });
        Some(target)
    }

    /// Whether `name` is bound at chunk top to a confirmed plain-data
    /// shape — a `const`-bound plain object/array literal that no
    /// statement in this chunk writes through. Member reads `name.k`
    /// / `name[pure]` on such a binding are pure (see
    /// `ChunkBinding::PlainData`).
    pub(crate) fn is_plain_data(&self, name: &str) -> bool {
        matches!(self.bindings.get(name), Some(ChunkBinding::PlainData))
    }

    pub(crate) fn plain_data_dependencies(
        &self,
        name: &str,
    ) -> Option<&crate::analysis_inputs::ProofDependencies> {
        self.plain_data_dependencies.get(name)
    }

    pub(crate) fn plain_data_inference_uses(
        &self,
        name: &str,
    ) -> Option<&BTreeSet<crate::reports::schema::InferenceUse>> {
        self.plain_data_inference_uses.get(name)
    }

    /// Whether `name` is a static ordinary array binding suitable for
    /// array-method at-init fallback refinement.
    pub(crate) fn is_plain_array(&self, name: &str) -> bool {
        self.plain_array_bindings.contains(name)
    }

    pub(crate) fn ts_enum_named_data_candidates(&self) -> &BTreeMap<Id, TsEnumNamedDataCandidate> {
        &self.ts_enum_named_data_candidates
    }

    pub(crate) fn set_iterable_dependencies(&self, span: Span) -> Option<&ProofDependencies> {
        self.set_iterable_proof.dependencies_for_constructor(span)
    }

    pub(crate) fn stable_set_spread_dependencies(&self, span: Span) -> Option<&ProofDependencies> {
        self.set_iterable_proof.stable_spread_dependencies(span)
    }

    pub(crate) fn stable_array_spread_dependencies(
        &self,
        operand: Span,
    ) -> Option<&ProofDependencies> {
        self.set_iterable_proof
            .stable_array_spread_dependencies(operand)
    }

    pub(crate) fn local_class_construction_dependencies(
        &self,
        span: Span,
    ) -> Option<&ProofDependencies> {
        self.local_class_construction_proof
            .dependencies_for_construction(span)
    }

    pub(crate) fn is_declared_pure_new(&self, name: &str) -> bool {
        self.pure_new_constructors.contains(name)
    }

    /// Cross-module purity verdict for an imported function binding,
    /// when the program-level oracle resolved one.
    pub(crate) fn imported_purity(&self, name: &str) -> Option<&Arc<Purity>> {
        self.imported_purities.get(name)
    }

    /// Whether `<recv>.<prop>(args)` is admitted as pure by an author
    /// `pure_members: [<prop>, …]` annotation on the binding `recv`.
    /// Args still classified independently — declared purity covers
    /// the function value, not its arguments.
    pub(crate) fn is_declared_pure_member(&self, recv: &str, prop: &str) -> bool {
        self.declared_pure_members
            .get(recv)
            .is_some_and(|props| props.contains(prop))
    }

    /// Whether `name` is a fluent-trusted root (author-asserted via
    /// `fluent_exports`, or a chunk-top `const` derived from one — see
    /// `collect_fluent_const_bindings`).
    pub(crate) fn is_fluent_binding(&self, name: &str) -> bool {
        self.fluent_bindings.contains(name)
    }
}

#[derive(Debug, Clone)]
struct ChunkFunction<'a> {
    key: u32,
    name: String,
    /// Parameter patterns. Default-value expressions evaluate at call
    /// time and destructuring patterns fire getters / the iterator
    /// protocol on the argument, so they participate in the body's
    /// purity classification.
    params: Vec<&'a Pat>,
    /// Block-bodied function/arrow.
    block_body: Option<&'a FunctionBody>,
    /// Concise-arrow expression body (`(x) => expr`).
    expr_body: Option<&'a Expr>,
    /// Every binding-ident name introduced by this function — its
    /// params plus every `BindingIdent` declared anywhere in its body
    /// (vars, nested function params, catch bindings, …). Over-approx
    /// is sound: these names lexically shadow chunk-top bindings,
    /// whitelist receivers, and spec-annotated names, so references
    /// to them inside the body must not resolve through the global
    /// tables / chunk graph.
    param_and_body_bindings: BTreeSet<String>,
}

impl ChunkFunction<'_> {
    /// Drive a `Visit` visitor over this function's parameter
    /// patterns and body. Params are included because default-value
    /// expressions evaluate at call time exactly like body code —
    /// both the purity walk (`BodyPurityCollector`) and the
    /// obligation capture must see them. Block
    /// bodies recurse via `visit_with`; concise-arrow expression
    /// bodies fire `visit_expr` directly so the visitor's
    /// `visit_call_expr` / `visit_expr` overrides catch the body.
    fn visit_body_with<V: Visit + ?Sized>(&self, visitor: &mut V) {
        for pat in &self.params {
            pat.visit_with(visitor);
        }
        if let Some(block) = self.block_body {
            block.visit_with(visitor);
        }
        if let Some(expr) = self.expr_body {
            expr.visit_with(visitor);
        }
    }
}

/// Collects every `BindingIdent` name it visits. Used to gather the
/// names a function introduces (params + body declarations) so reads
/// on a like-named chunk-top PlainData candidate aren't mis-classified
/// as pure when the candidate is lexically shadowed.
struct BindingNameCollector {
    names: BTreeSet<String>,
}

impl Visit for BindingNameCollector {
    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.names.insert(node.id.sym.to_string());
        node.visit_children_with(self);
    }
}

/// Names bound by `params` and by `body` (the function being
/// classified). Conservative over-approximation: collects every
/// `BindingIdent` (including those in nested scopes) — sound because
/// extra names only ever cause more reads to be treated as impure.
fn collect_function_bindings<'p>(
    params: impl Iterator<Item = &'p Pat>,
    block_body: Option<&FunctionBody>,
    expr_body: Option<&Expr>,
) -> BTreeSet<String> {
    let mut collector = BindingNameCollector {
        names: BTreeSet::new(),
    };
    for pat in params {
        pat.visit_with(&mut collector);
    }
    if let Some(block) = block_body {
        block.visit_with(&mut collector);
    }
    if let Some(expr) = expr_body {
        expr.visit_with(&mut collector);
    }
    collector.names
}

fn collect_chunk_functions<'a, 'item>(
    body: &'a [TopLevelItemView<'item>],
) -> Vec<ChunkFunction<'a>> {
    let mut out = Vec::new();
    for item in body {
        match item.as_module_item() {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(fn_decl))) => push_fn_decl(fn_decl, &mut out),
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => push_var_functions(var, &mut out),
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
                Decl::Fn(fn_decl) => push_fn_decl(fn_decl, &mut out),
                Decl::Var(var) => push_var_functions(var, &mut out),
                _ => {}
            },
            _ => {}
        }
    }
    out
}

fn push_fn_decl<'a>(fn_decl: &'a FnDecl, out: &mut Vec<ChunkFunction<'a>>) {
    let block_body = fn_decl.function.body.as_ref();
    out.push(ChunkFunction {
        key: fn_decl.function.span.lo.0,
        name: fn_decl.ident.sym.to_string(),
        params: fn_decl.function.params.iter().map(|p| &p.pat).collect(),
        block_body,
        expr_body: None,
        param_and_body_bindings: collect_function_bindings(
            fn_decl.function.params.iter().map(|p| &p.pat),
            block_body,
            None,
        ),
    });
}

fn push_var_functions<'a>(var: &'a VarDecl, out: &mut Vec<ChunkFunction<'a>>) {
    // `let` / `var` bindings are reassignable: caching their
    // body's purity and short-circuiting `f(...)` to that purity
    // is unsound if a later `f = …` reassigns them to something
    // impure. Only `const`-bound function/arrow initializers are
    // tracked; reassignment of a `const` is a syntax error.
    if var.kind != VarDeclKind::Const {
        return;
    }
    for decl in &var.decls {
        let Pat::Ident(binding) = &decl.name else {
            continue;
        };
        let Some(init) = decl.init.as_deref() else {
            continue;
        };
        let name = binding.id.sym.to_string();
        match init {
            Expr::Fn(fn_expr) => {
                let block_body = fn_expr.function.body.as_ref();
                out.push(ChunkFunction {
                    key: fn_expr.function.span.lo.0,
                    name,
                    params: fn_expr.function.params.iter().map(|p| &p.pat).collect(),
                    block_body,
                    expr_body: None,
                    param_and_body_bindings: collect_function_bindings(
                        fn_expr.function.params.iter().map(|p| &p.pat),
                        block_body,
                        None,
                    ),
                });
            }
            Expr::Arrow(arrow) => match arrow.body.as_ref() {
                ArrowFunctionBody::FunctionBody(block) => {
                    out.push(ChunkFunction {
                        key: arrow.span.lo.0,
                        name,
                        params: arrow.params.iter().collect(),
                        block_body: Some(block),
                        expr_body: None,
                        param_and_body_bindings: collect_function_bindings(
                            arrow.params.iter(),
                            Some(block),
                            None,
                        ),
                    });
                }
                ArrowFunctionBody::Expr(expr) => {
                    out.push(ChunkFunction {
                        key: arrow.span.lo.0,
                        name,
                        params: arrow.params.iter().collect(),
                        block_body: None,
                        expr_body: Some(expr.as_ref()),
                        param_and_body_bindings: collect_function_bindings(
                            arrow.params.iter(),
                            None,
                            Some(expr.as_ref()),
                        ),
                    });
                }
            },
            _ => {}
        }
    }
}

fn classify_function_body(
    function: &ChunkFunction<'_>,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    // Names bound by this function (its params and every binding
    // declared anywhere in its body) lexically shadow EVERY outer
    // resolution path of the same name: chunk-top PlainData
    // candidates, chunk-top function bindings, whitelist receivers
    // (`Math`, `Object`, …), pure-new builtins (`Map`, `Set`, …),
    // and spec-annotated `declared_pure` / `pure_members` /
    // `pure_new` bindings. A reference to a shadowed name inside
    // the body is a *different value* than the one the global
    // tables / chunk graph / spec author describe, so none of those
    // shortcuts may fire for it. Over-approximation (collecting
    // bindings from nested scopes too) is sound: extra names only
    // make more reads conservative. Lexical scope is constant
    // across one classification tree (nested function/arrow bodies
    // are not walked), so this single set suffices for the whole
    // body.
    let local_shadowed = &function.param_and_body_bindings;
    // Destructuring parameters fire getters (object patterns) or
    // the iterator protocol (array patterns) on the caller's
    // argument at call time — user code the body's expression walk
    // never sees. A function with any destructuring param is not
    // pure-callable. Default-value expressions are classified by
    // the body walk below (`visit_body_with` drives the visitor
    // over the param patterns too).
    let param_purity = function
        .params
        .iter()
        .filter_map(|pat| param_destructuring_purity(pat))
        .fold(Purity::pure(), Purity::worst);
    let mut collector = BodyPurityCollector {
        purity: Purity::pure(),
        fresh_record: graph.fresh_record_proof(function.key),
        shadowed,
        local_shadowed,
        declared_pure,
        graph,
    };
    function.visit_body_with(&mut collector);
    param_purity.worst(collector.purity)
}

fn classify_function_body_transfer(
    function: &ChunkFunction<'_>,
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
    locate: &mut Option<&mut dyn FnMut(Span) -> Option<SourceLocation>>,
) -> PurityBodyTransfer {
    let obligations = graph
        .purity_call_obligations
        .as_ref()
        .expect("transfer extraction configures an obligation buffer");
    obligations.borrow_mut().clear();
    let mut purity = classify_function_body(function, shadowed, declared_pure, graph);
    if let Purity::NotPure { reasons } = &mut purity
        && let Some(locate) = locate.as_deref_mut()
    {
        for reason in reasons {
            reason.source_location = locate(reason.span);
        }
    }
    let mut captured = obligations.borrow_mut();
    let mut obligations = std::mem::take(&mut *captured);
    drop(captured);
    if let Some(locate) = locate.as_deref_mut() {
        for obligation in &mut obligations {
            obligation.source_location = locate(obligation.span);
        }
    }
    PurityBodyTransfer::from_classification(purity, obligations)
}

/// `Some(NotPure)` when `pat` contains a destructuring pattern that
/// fires user code on the bound value at evaluation time: object
/// patterns run `[[Get]]` per key (user getters / proxy traps),
/// array patterns run the iterator protocol. Plain idents and rest
/// params (fresh array from the arguments list) contribute nothing;
/// default-value expressions are classified separately as
/// expressions by the body walk.
pub(crate) fn param_destructuring_purity(pat: &Pat) -> Option<Purity> {
    match pat {
        Pat::Ident(_) => None,
        Pat::Rest(rest) => param_destructuring_purity(&rest.arg),
        Pat::Assign(assign) => param_destructuring_purity(&assign.left),
        Pat::Array(arr) => Some(Purity::from_reason_with_detail(
            PurityRule::DestructuringPattern,
            arr.span,
            "array destructuring fires the iterator protocol on the bound value".to_string(),
        )),
        Pat::Object(obj) => Some(Purity::from_reason_with_detail(
            PurityRule::DestructuringPattern,
            obj.span,
            "object destructuring fires [[Get]] (user getters / proxy traps) on the bound value"
                .to_string(),
        )),
        Pat::Invalid(_) | Pat::Expr(_) => Some(Purity::from_reason(PurityRule::Other, pat.span())),
    }
}

/// Visitor that walks a function body and accumulates the worst
/// purity of every top-level expression encountered. Skips nested
/// function/arrow/method/getter/setter bodies (those are separate
/// lazy scopes — their purity, if needed, comes from their own
/// graph entry or from the caller's `Unknown` fallback).
struct BodyPurityCollector<'a> {
    purity: Purity,
    fresh_record: Option<&'a FreshRecordProof>,
    shadowed: &'a BTreeSet<&'static str>,
    /// Every name bound by the function whose body is being walked
    /// (its params / body declarations). References to such names
    /// must not resolve through any global table, chunk-graph
    /// binding, or spec annotation — the lexical binding is a
    /// different value than the outer one those describe.
    local_shadowed: &'a BTreeSet<String>,
    declared_pure: &'a BTreeSet<String>,
    graph: &'a ChunkCodeGraph,
}

impl Visit for BodyPurityCollector<'_> {
    fn visit_function(&mut self, _: &Function) {}
    fn visit_arrow_expr(&mut self, _: &ArrowExpr) {}
    fn visit_method_prop(&mut self, _: &MethodProp) {}
    fn visit_getter_prop(&mut self, _: &GetterProp) {}
    fn visit_setter_prop(&mut self, _: &SetterProp) {}

    fn visit_expr(&mut self, expr: &Expr) {
        // Classify the entire expression in one shot —
        // `classify_expr_purity` already recurses through
        // nested subexpressions and returns the worst.
        let classified = if let Expr::Assign(assign) = expr.unwrap_parens()
            && self
                .fresh_record
                .is_some_and(|proof| proof.write_spans.contains(&assign.span.lo.0))
        {
            // The shared certificate proved this is a write to an own,
            // writable data field on the newly allocated local record.
            assign.right.as_ref()
        } else {
            expr
        };
        let p = classify_expr_purity(
            classified,
            self.shadowed,
            self.local_shadowed,
            self.declared_pure,
            self.graph,
        );
        self.purity = std::mem::replace(&mut self.purity, Purity::pure()).worst(p);
    }

    // Statement-level effects that don't surface as an Impure /
    // Unknown sub-expression. `throw e` alters control flow
    // observably even when `e` is a Pure literal; `debugger`
    // pauses execution observably to a host attached to the
    // process. Both make the enclosing function not Pure.
    fn visit_throw_stmt(&mut self, node: &ThrowStmt) {
        let throw_reason = Purity::from_reason(PurityRule::ThrowStmt, node.span);
        self.purity = std::mem::replace(&mut self.purity, Purity::pure()).worst(throw_reason);
        // Still recurse so the thrown expression contributes its
        // own purity (e.g. `throw io()` should also see the call).
        node.arg.visit_with(self);
    }

    fn visit_debugger_stmt(&mut self, node: &DebuggerStmt) {
        let dbg_reason = Purity::from_reason(PurityRule::DebuggerStmt, node.span);
        self.purity = std::mem::replace(&mut self.purity, Purity::pure()).worst(dbg_reason);
    }

    // Iteration statements fire user code beyond their
    // sub-expressions: `for-of` (and `for await-of`) runs the
    // iterated value's `[Symbol.iterator]` / `[Symbol.asyncIterator]`
    // protocol; `for-in` runs `[[OwnPropertyKeys]]` /
    // `[[GetOwnPropertyDescriptor]]` per step, which a Proxy traps.
    // The RHS expression's own evaluation is classified by the
    // regular recursion, but the protocol firing is invisible to it
    // — flag the statement itself.
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        let reason = Purity::from_reason_with_detail(
            PurityRule::IterationProtocol,
            node.span,
            "for-of fires the iterator protocol on the iterated value".to_string(),
        );
        self.purity = std::mem::replace(&mut self.purity, Purity::pure()).worst(reason);
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        let reason = Purity::from_reason_with_detail(
            PurityRule::IterationProtocol,
            node.span,
            "for-in enumeration can fire proxy traps on the enumerated value".to_string(),
        );
        self.purity = std::mem::replace(&mut self.purity, Purity::pure()).worst(reason);
        node.visit_children_with(self);
    }

    // `const {a} = o` / `const [x] = o` inside a body fire getters /
    // the iterator protocol on the initializer value — effects the
    // init expression's own classification doesn't cover.
    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Some(reason) = param_destructuring_purity(&node.name) {
            self.purity = std::mem::replace(&mut self.purity, Purity::pure()).worst(reason);
        }
        node.visit_children_with(self);
    }
}
