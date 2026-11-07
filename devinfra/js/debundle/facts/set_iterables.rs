//! Narrow structural proofs for standard `Set` construction from fresh arrays.
//!
//! The proof is shared by purity classification and synchronous callback
//! analysis. It only records exact constructor spans; consumers still apply
//! their own surrounding purity/effect checks.

use super::*;
use crate::analysis_inputs::{
    InferredSetConstruction, InferredSetOperation, NativeInputKind, NativeOperationInputEvidence,
    ProofDependencies, ResolvedNativeOperationInput, StableSetIterableUse,
};
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::semantic_inference::{SemanticFacts, SemanticKey};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use anyhow::{Result, bail};
use source_identity::SourceUnitOrdinal;
use std::sync::Arc;

/// Source identity and top-level initialization scope for one checked
/// collection constructor operand. Construction is private to the source
/// proof that joins an exact top-level operation with its checked input. This
/// records syntax/proof identity only; it never contains evaluated iterator
/// yields or a runtime collection size.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct TopLevelNativeIterableOperandProjection {
    operation_span: Span,
    operand: NativeIterableOperandOrigin,
    operation_owner: SourceUnitOrdinal,
}

/// Origin of the statically checked constructor operand. Runtime iteration
/// still resolves and validates the corresponding allocation at its prefix.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum NativeIterableOperandOrigin {
    ImmediateArrayLiteral {
        allocation_site: u32,
    },
    BorrowedConstArray {
        source_binding: Id,
        allocation_site: u32,
        allocation_owner: SourceUnitOrdinal,
    },
}

impl TopLevelNativeIterableOperandProjection {
    fn new(
        operation_span: Span,
        operand: NativeIterableOperandOrigin,
        operation_owner: SourceUnitOrdinal,
    ) -> Self {
        Self {
            operation_span,
            operand,
            operation_owner,
        }
    }

    pub(crate) fn operation_span(&self) -> Span {
        self.operation_span
    }

    pub(crate) fn operand_origin(&self) -> &NativeIterableOperandOrigin {
        &self.operand
    }

    pub(crate) fn operation_owner(&self) -> SourceUnitOrdinal {
        self.operation_owner
    }

    pub(crate) fn matches_operand(&self, expr: &Expr) -> bool {
        matches!(
            (&self.operand, expr.unwrap_parens()),
            (
                NativeIterableOperandOrigin::ImmediateArrayLiteral { allocation_site },
                Expr::Array(array)
            ) if array.span.lo.0 == *allocation_site
        ) || matches!(
            (&self.operand, expr.unwrap_parens()),
            (
                NativeIterableOperandOrigin::BorrowedConstArray { source_binding, .. },
                Expr::Ident(ident)
            ) if ident.to_id() == *source_binding
        )
    }
}

#[derive(Debug, Clone)]
pub(crate) struct TopLevelNativeIterableOperandSite {
    pub(crate) operation_span: Span,
    pub(crate) operand: NativeIterableOperandOrigin,
    pub(crate) operation_owner: SourceUnitOrdinal,
}

/// Certify the one-way source/schedule premise before value-flow inference.
/// The input constructor and every intervening source unit are independently
/// selected primitive-Set operations in the same original declaration. No
/// graph result, flow identity or assignment is consulted here; the interval
/// must stay in one output module, which the gate stage enforces.
pub fn certify_stable_set_spreads(
    inputs: &[ResolvedNativeOperationInput],
) -> Result<BTreeMap<u32, StableSetIterableUse>> {
    let by_ordinal: BTreeMap<_, _> = inputs
        .iter()
        .filter(|input| {
            input.evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
        })
        .map(|input| (input.initializer_ordinal.0, input))
        .collect();
    let mut stable = BTreeMap::new();
    for spread in inputs.iter().filter(|input| {
        input.evidence.operation
            == NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread)
    }) {
        let Some(source) = spread.spread_source.as_ref() else {
            bail!(
                "{} has no exact Set spread source",
                spread.evidence.spec_location
            );
        };
        let seed = by_ordinal.values().find(|input| {
            input.binding == *source
                && input.initializer_ordinal < spread.initializer_ordinal
                && input.selected_statement_span == spread.selected_statement_span
        });
        let Some(seed) = seed else {
            bail!(
                "{} requires an earlier certified fresh primitive Set in the same original declaration",
                spread.evidence.spec_location
            );
        };
        let mut dependencies = ProofDependencies::default();
        let mut interval_bindings = Vec::new();
        dependencies
            .native_operation_inputs
            .insert(spread.evidence.reference.clone());
        for ordinal in seed.initializer_ordinal.0..spread.initializer_ordinal.0 {
            let Some(input) = by_ordinal.get(&ordinal) else {
                bail!(
                    "{} has an uncertified intervening source unit",
                    spread.evidence.spec_location
                );
            };
            if input.selected_statement_span != spread.selected_statement_span {
                bail!(
                    "{} has an intervening Set constructor outside its original declaration",
                    spread.evidence.spec_location
                );
            }
            dependencies
                .native_operation_inputs
                .insert(input.evidence.reference.clone());
            interval_bindings.push(input.binding.clone());
        }
        interval_bindings.push(spread.binding.clone());
        stable.insert(
            spread.call_span.lo.0,
            StableSetIterableUse {
                source_span: spread
                    .spread_source_span
                    .expect("validated spread has operand span"),
                spread_token_span: spread
                    .spread_token_span
                    .expect("validated spread has token span"),
                interval_bindings,
                dependencies,
            },
        );
    }
    Ok(stable)
}

#[derive(Debug, Clone, Default)]
pub(crate) struct SetIterableShapes {
    pub(super) constructor_starts: BTreeSet<u32>,
    borrowed_const_array_operands: BTreeMap<u32, NativeIterableOperandOrigin>,
}

impl SetIterableShapes {
    pub(crate) fn analyze(
        body: &[TopLevelItemView<'_>],
        shadowed: &BTreeSet<&'static str>,
        flow: &ChunkValueFlow,
        plain_data_bindings: &[String],
        dynamic_scope: DynamicScopeFact,
    ) -> Self {
        if dynamic_scope.present || shadowed.contains("Set") {
            return Self::default();
        }
        let candidates = collect_const_primitive_arrays(body);
        let mut scanner = ArrayUseScanner {
            candidates: &candidates,
            flow,
            shadowed,
            invalid: BTreeSet::new(),
            alias_consumers: Vec::new(),
            direct_literal_calls: BTreeSet::new(),
        };
        for item in body {
            item.as_module_item().visit_with(&mut scanner);
        }

        let mut alias_neighbors = BTreeMap::<Id, BTreeSet<Id>>::new();
        for (alias, candidate) in &candidates {
            if let Some(source) = &candidate.source {
                alias_neighbors
                    .entry(alias.clone())
                    .or_default()
                    .insert(source.clone());
                alias_neighbors
                    .entry(source.clone())
                    .or_default()
                    .insert(alias.clone());
            }
        }
        let rules: Vec<_> = alias_neighbors.keys().cloned().collect();
        let adjacency = alias_neighbors
            .into_iter()
            .map(|(id, neighbors)| (id, neighbors.into_iter().collect()))
            .collect();
        let mut inference = IterativeInference::new(AliasInvalidationRules { adjacency });
        for id in scanner.invalid {
            inference.seed(id, ());
        }
        let invalid: BTreeSet<_> = inference.solve(rules).into_values().into_keys().collect();

        let mut constructor_starts = scanner.direct_literal_calls;
        let mut borrowed_const_array_operands = BTreeMap::new();
        for (array, start) in scanner.alias_consumers {
            if !invalid.contains(&array) {
                constructor_starts.insert(start);
                if let Some(candidate) = candidates.get(&array) {
                    borrowed_const_array_operands.insert(
                        start,
                        NativeIterableOperandOrigin::BorrowedConstArray {
                            source_binding: array,
                            allocation_site: candidate.allocation_site,
                            allocation_owner: candidate.allocation_owner,
                        },
                    );
                }
            }
        }
        constructor_starts.extend(proven_static_member_sets(
            body,
            shadowed,
            flow,
            plain_data_bindings,
        ));
        Self {
            constructor_starts,
            borrowed_const_array_operands,
        }
    }

    pub(crate) fn borrowed_const_array_operand(
        &self,
        operation_span: Span,
    ) -> Option<&NativeIterableOperandOrigin> {
        self.borrowed_const_array_operands.get(&operation_span.lo.0)
    }
}

struct AliasInvalidationRules {
    adjacency: BTreeMap<Id, Vec<Id>>,
}

impl IterativeRules for AliasInvalidationRules {
    type Key = Id;
    type Fact = ();
    type Rule = Id;

    fn evaluate(&self, id: &Id, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        if context.read(id.clone()).is_none() {
            return;
        }
        for neighbor in self.adjacency.get(id).into_iter().flatten() {
            context.propose(neighbor.clone(), ());
        }
    }

    fn merge(_key: &Self::Key, _current: &mut Self::Fact, _contribution: Self::Fact) -> bool {
        false
    }
}

#[derive(Debug, Clone, Default)]
pub(crate) struct SetIterableProof {
    facts: Arc<SemanticFacts>,
    author_inputs: BTreeMap<u32, NativeOperationInputEvidence>,
    author_dependencies: BTreeMap<u32, ProofDependencies>,
    stable_spreads: BTreeMap<u32, ProofDependencies>,
    source_constructions: BTreeMap<u32, InferredSetConstruction>,
    native_iterable_operands: BTreeMap<u32, TopLevelNativeIterableOperandProjection>,
    required_native_iterable_operands: BTreeSet<u32>,
    protected_intervals: Vec<Vec<Id>>,
    spread_sources: BTreeMap<u32, u32>,
    array_spreads: StableArraySpreadShapes,
}

impl SetIterableProof {
    pub(super) fn new(
        facts: Arc<SemanticFacts>,
        author_inputs: BTreeMap<u32, NativeOperationInputEvidence>,
        stable_spreads: &BTreeMap<u32, StableSetIterableUse>,
        source_constructions: &BTreeMap<u32, InferredSetConstruction>,
        native_inputs: &[ResolvedNativeOperationInput],
        array_spreads: StableArraySpreadShapes,
        constructor_operands: &BTreeMap<u32, TopLevelNativeIterableOperandSite>,
    ) -> Self {
        let author_dependencies = author_inputs
            .iter()
            .map(|(site, evidence)| {
                (
                    *site,
                    stable_spreads.get(site).map_or_else(
                        || ProofDependencies {
                            native_operation_inputs: BTreeSet::from([evidence.reference.clone()]),
                            ..ProofDependencies::default()
                        },
                        |stable| stable.dependencies.clone(),
                    ),
                )
            })
            .collect();
        let spreads: Vec<_> = stable_spreads
            .values()
            .chain(
                source_constructions
                    .values()
                    .filter_map(|proof| match &proof.operation {
                        InferredSetOperation::ProtectedSpread { interval, .. } => {
                            Some(interval.as_ref())
                        }
                        InferredSetOperation::PrimitiveArray => None,
                    }),
            )
            .collect();
        let protected_intervals = spreads
            .iter()
            .map(|spread| spread.interval_bindings.clone())
            .collect();
        let origins: BTreeMap<_, _> = native_inputs
            .iter()
            .map(|input| (input.binding.clone(), input.call_span.lo.0))
            .chain(
                source_constructions
                    .iter()
                    .map(|(site, proof)| (proof.binding.clone(), *site)),
            )
            .collect();
        let spread_sources = spreads
            .iter()
            .filter_map(|spread| {
                let source = origins.get(spread.interval_bindings.first()?)?;
                Some(
                    [spread.source_span.lo.0, spread.spread_token_span.lo.0]
                        .into_iter()
                        .map(|span| (span, *source)),
                )
            })
            .flatten()
            .collect();
        let mut proof = Self {
            facts,
            author_inputs,
            author_dependencies,
            protected_intervals,
            spread_sources,
            source_constructions: source_constructions.clone(),
            native_iterable_operands: BTreeMap::new(),
            required_native_iterable_operands: constructor_operands
                .iter()
                .filter_map(|(site, source)| {
                    matches!(
                        &source.operand,
                        NativeIterableOperandOrigin::BorrowedConstArray { .. }
                    )
                    .then_some(*site)
                })
                .collect(),
            stable_spreads: spreads
                .into_iter()
                .flat_map(|stable| {
                    [stable.source_span.lo.0, stable.spread_token_span.lo.0]
                        .into_iter()
                        .map(|site| (site, stable.dependencies.clone()))
                })
                .collect(),
            array_spreads,
        };
        let operation_sites: BTreeSet<_> = proof
            .author_inputs
            .keys()
            .chain(proof.source_constructions.keys())
            .chain(constructor_operands.iter().filter_map(|(site, source)| {
                matches!(
                    &source.operand,
                    NativeIterableOperandOrigin::BorrowedConstArray { .. }
                )
                .then_some(site)
            }))
            .copied()
            .collect();
        for site in operation_sites {
            let Some(source) = constructor_operands.get(&site) else {
                continue;
            };
            if proof
                .dependencies_for_constructor(source.operation_span)
                .is_none()
            {
                continue;
            }
            if matches!(
                &source.operand,
                NativeIterableOperandOrigin::BorrowedConstArray {
                    allocation_owner,
                    ..
                } if allocation_owner.0 >= source.operation_owner.0
            ) {
                continue;
            }
            if source.operation_span.lo.0 != site {
                continue;
            }
            proof.native_iterable_operands.insert(
                site,
                TopLevelNativeIterableOperandProjection::new(
                    source.operation_span,
                    source.operand.clone(),
                    source.operation_owner,
                ),
            );
        }
        proof
    }

    pub(crate) fn protected_intervals(&self) -> &[Vec<Id>] {
        &self.protected_intervals
    }

    pub(crate) fn stable_spread_source(&self, span: Span) -> Option<u32> {
        self.spread_sources.get(&span.lo.0).copied()
    }

    pub(crate) fn stable_spread_dependencies(&self, span: Span) -> Option<&ProofDependencies> {
        self.stable_spreads.get(&span.lo.0)
    }

    pub(crate) fn dependencies_for_constructor(&self, span: Span) -> Option<&ProofDependencies> {
        self.author_dependencies
            .get(&span.lo.0)
            .or_else(|| {
                self.source_constructions
                    .get(&span.lo.0)
                    .map(|proof| &proof.dependencies)
            })
            .or_else(|| {
                self.facts
                    .dependencies(&SemanticKey::PrimitiveSetIteration(span.lo.0))
            })
    }

    /// Return the one shared source/operand projection and its canonical
    /// protocol dependencies for a checked top-level Set constructor.
    pub(crate) fn native_iterable_operand(
        &self,
        span: Span,
    ) -> Option<(&TopLevelNativeIterableOperandProjection, &ProofDependencies)> {
        let projection = self.native_iterable_operands.get(&span.lo.0)?;
        (projection.operation_span() == span)
            .then_some((projection, self.dependencies_for_constructor(span)?))
    }

    pub(crate) fn requires_native_iterable_operand(&self, span: Span) -> bool {
        self.required_native_iterable_operands.contains(&span.lo.0)
    }

    /// The projection constrains literals that use the shared certificate.
    /// Previously certified nonliteral and unrelated fresh-Set sites keep
    /// their existing flow-root admission and do not acquire a new owner gate.
    pub(crate) fn heap_constructor_in_owner(&self, span: Span, owner: SourceUnitOrdinal) -> bool {
        match self.native_iterable_operand(span) {
            Some((projection, _)) => projection.operation_owner() == owner,
            None => !self.requires_native_iterable_operand(span),
        }
    }

    /// Return canonical proof dependencies for early heap admission. Immediate
    /// literals and checked const-array operands use the shared projection;
    /// primitive static-member constructions retain their existing path.
    pub(crate) fn heap_constructor_dependencies(
        &self,
        span: Span,
        owner: SourceUnitOrdinal,
    ) -> Option<&ProofDependencies> {
        if !self.heap_constructor_in_owner(span, owner) {
            return None;
        }
        self.dependencies_for_constructor(span)
    }

    pub(crate) fn author_input_for_constructor(
        &self,
        span: Span,
    ) -> Option<&NativeOperationInputEvidence> {
        self.author_inputs.get(&span.lo.0)
    }

    pub(crate) fn inference_use_for_constructor(
        &self,
        span: Span,
    ) -> Option<crate::analysis_provenance::InferenceUse> {
        self.source_constructions
            .get(&span.lo.0)
            .map(|proof| proof.inference.clone())
            .or_else(|| {
                self.facts
                    .inference_use(&SemanticKey::PrimitiveSetIteration(span.lo.0))
            })
    }

    pub(crate) fn stable_array_spread_dependencies(
        &self,
        token: Span,
    ) -> Option<&ProofDependencies> {
        let initializer = self.array_spreads.by_site.get(&token.lo.0)?;
        self.facts
            .dependencies(&SemanticKey::StablePrimitiveArraySpread(*initializer))
    }

    pub(crate) fn stable_array_spread_source(&self, span: Span) -> Option<u32> {
        self.array_spreads.source_origins.get(&span.lo.0).copied()
    }

    pub(crate) fn stable_array_spread_inference_use(
        &self,
        token: Span,
    ) -> Option<crate::analysis_provenance::InferenceUse> {
        let initializer = self.array_spreads.by_site.get(&token.lo.0)?;
        self.facts
            .inference_use(&SemanticKey::StablePrimitiveArraySpread(*initializer))
    }

    pub(crate) fn stable_iterable_producers(
        &self,
        consumer: SourceUnitOrdinal,
    ) -> Option<&BTreeSet<SourceUnitOrdinal>> {
        self.array_spreads
            .by_initializer
            .iter()
            .find_map(|(initializer, shape)| {
                (shape.consumer == consumer
                    && self
                        .facts
                        .dependencies(&SemanticKey::StablePrimitiveArraySpread(*initializer))
                        .is_some())
                .then_some(&shape.producers)
            })
    }
}

/// A chunk-top `const` object literal has an own primitive data property at
/// each recorded key. `PlainData` separately proves that its identity neither
/// escapes nor receives writes/accessors elsewhere in the chunk. Both proofs
/// are needed: a pure member read can still return a callback or iterator.
fn primitive_object_properties(
    body: &[TopLevelItemView<'_>],
    plain_data_bindings: &[String],
) -> BTreeMap<u32, BTreeSet<String>> {
    let plain_names: BTreeSet<&str> = plain_data_bindings.iter().map(String::as_str).collect();
    let mut objects = BTreeMap::new();
    for item in body {
        let ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) = item.as_module_item() else {
            continue;
        };
        if var.kind != VarDeclKind::Const {
            continue;
        }
        for decl in &var.decls {
            let Pat::Ident(binding) = &decl.name else {
                continue;
            };
            if !plain_names.contains(binding.id.sym.as_ref()) {
                continue;
            }
            let Some(Expr::Object(object)) = decl.init.as_deref().map(Expr::unwrap_parens) else {
                continue;
            };
            if let Some(primitive_keys) = primitive_literal_property_keys(object) {
                objects.insert(object.span.lo.0, primitive_keys);
            }
        }
    }
    objects
}

/// Own literal primitive values of an ordinary object, after duplicate-key
/// replacement. Reject shapes whose keys or descriptors need runtime reasoning.
pub(crate) fn primitive_literal_property_keys(object: &ObjectLit) -> Option<BTreeSet<String>> {
    let mut primitive_keys = BTreeSet::new();
    for prop in &object.props {
        let PropOrSpread::Prop(prop) = prop else {
            return None;
        };
        let Prop::KeyValue(kv) = prop.as_ref() else {
            return None;
        };
        let key = match &kv.key {
            PropName::Ident(name) => name.sym.to_string(),
            PropName::Str(name) => name.value.to_string_lossy().into_owned(),
            _ => return None,
        };
        if key == "__proto__" {
            return None;
        }
        if is_primitive_literal(&kv.value) {
            primitive_keys.insert(key);
        } else {
            primitive_keys.remove(&key);
        }
    }
    Some(primitive_keys)
}

fn is_primitive_literal(expr: &Expr) -> bool {
    matches!(
        expr.unwrap_parens(),
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_))
    )
}

fn proven_static_member_sets(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
    flow: &ChunkValueFlow,
    plain_data_bindings: &[String],
) -> BTreeSet<u32> {
    let objects = primitive_object_properties(body, plain_data_bindings);
    if objects.is_empty() {
        return BTreeSet::new();
    }
    let mut proven = BTreeSet::new();
    for item in body {
        let ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) = item.as_module_item() else {
            continue;
        };
        for decl in &var.decls {
            let Some(Expr::New(new)) = decl.init.as_deref().map(Expr::unwrap_parens) else {
                continue;
            };
            if !is_standard_set_constructor(&new.callee, shadowed, flow) {
                continue;
            }
            let Some([ExprOrSpread { spread: None, expr }]) = new.args.as_deref() else {
                continue;
            };
            let Expr::Array(array) = expr.unwrap_parens() else {
                continue;
            };
            let mut has_member = false;
            let all_primitive = array.elems.iter().all(|element| {
                let Some(ExprOrSpread { spread: None, expr }) = element else {
                    return false;
                };
                match expr.unwrap_parens() {
                    Expr::Member(member) => {
                        has_member = true;
                        let Expr::Ident(_) = member.obj.unwrap_parens() else {
                            return false;
                        };
                        let MemberProp::Ident(prop) = &member.prop else {
                            return false;
                        };
                        let mut receiver_values = flow.values_of_expr(&member.obj).into_iter();
                        let Some(Value::Alloc(Kind::Object, object)) = receiver_values.next()
                        else {
                            return false;
                        };
                        receiver_values.next().is_none()
                            && objects
                                .get(&object)
                                .is_some_and(|keys| keys.contains(prop.sym.as_ref()))
                    }
                    other => is_primitive_literal(other),
                }
            });
            if has_member && all_primitive {
                proven.insert(new.span.lo.0);
            }
        }
    }
    proven
}

#[derive(Debug)]
struct ArrayCandidate {
    source: Option<Id>,
    allocation_site: u32,
    allocation_owner: SourceUnitOrdinal,
}

fn collect_const_primitive_arrays(body: &[TopLevelItemView<'_>]) -> BTreeMap<Id, ArrayCandidate> {
    let mut candidates = BTreeMap::<Id, ArrayCandidate>::new();
    for (ordinal, item) in body.iter().enumerate() {
        let module_item = item.as_module_item();
        // An exported declaration is already published outside this chunk.
        let ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) = module_item else {
            continue;
        };
        if var.kind != VarDeclKind::Const || var.decls.len() != 1 {
            continue;
        }
        let decl = &var.decls[0];
        let Pat::Ident(binding) = &decl.name else {
            continue;
        };
        let Some(init) = decl.init.as_deref() else {
            continue;
        };
        let (source, allocation_site, allocation_owner) = match init.unwrap_parens() {
            Expr::Array(array) if array_is_primitive_literal(array) => {
                (None, array.span.lo.0, SourceUnitOrdinal(ordinal))
            }
            Expr::Ident(ident) if candidates.contains_key(&ident.to_id()) => {
                let source = &candidates[&ident.to_id()];
                (
                    Some(ident.to_id()),
                    source.allocation_site,
                    source.allocation_owner,
                )
            }
            _ => continue,
        };
        candidates.insert(
            binding.id.to_id(),
            ArrayCandidate {
                source,
                allocation_site,
                allocation_owner,
            },
        );
    }
    candidates
}

fn array_is_primitive_literal(array: &ArrayLit) -> bool {
    array.elems.iter().all(|element| {
        let Some(element) = element else {
            return false;
        };
        if element.spread.is_some() {
            return false;
        }
        matches!(
            element.expr.unwrap_parens(),
            Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_))
        )
    })
}

fn is_standard_set_constructor(
    callee: &Expr,
    shadowed: &BTreeSet<&'static str>,
    flow: &ChunkValueFlow,
) -> bool {
    !shadowed.contains("Set")
        && matches!(callee.unwrap_parens(), Expr::Ident(ident)
            if ident.sym.as_ref() == "Set" && !flow.is_declared(&ident.to_id()))
}

fn has_fresh_primitive_array_argument(args: &[ExprOrSpread]) -> bool {
    matches!(
        args,
        [ExprOrSpread { spread: None, expr }]
            if matches!(expr.unwrap_parens(), Expr::Array(array) if array_is_primitive_literal(array))
    )
}

struct ArrayUseScanner<'a> {
    candidates: &'a BTreeMap<Id, ArrayCandidate>,
    flow: &'a ChunkValueFlow,
    shadowed: &'a BTreeSet<&'static str>,
    invalid: BTreeSet<Id>,
    alias_consumers: Vec<(Id, u32)>,
    direct_literal_calls: BTreeSet<u32>,
}

impl Visit for ArrayUseScanner<'_> {
    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Pat::Ident(binding) = &node.name
            && let Some(candidate) = self.candidates.get(&binding.id.to_id())
        {
            // Candidate declaration identifiers are not value uses. An alias
            // initializer is also an approved reference to the same array;
            // a primitive array initializer has no candidate references.
            if candidate.source.is_none()
                && let Some(init) = node.init.as_deref()
            {
                init.visit_with(self);
            }
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        node.callee.visit_with(self);
        let args = node.args.as_deref().unwrap_or_default();
        if is_standard_set_constructor(&node.callee, self.shadowed, self.flow) {
            if has_fresh_primitive_array_argument(args) {
                self.direct_literal_calls.insert(node.span.lo.0);
            }
            if let [ExprOrSpread { spread: None, expr }] = args
                && let Expr::Ident(array) = expr.unwrap_parens()
                && self.candidates.contains_key(&array.to_id())
            {
                self.alias_consumers.push((array.to_id(), node.span.lo.0));
                return;
            }
        }
        if let Some(args) = node.args.as_deref() {
            args.visit_with(self);
        }
    }

    fn visit_ident(&mut self, node: &Ident) {
        let id = node.to_id();
        if self.candidates.contains_key(&id) {
            self.invalid.insert(id);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::{compute_shadowed_globals, top_level_item_views};
    use raw_js_test_support::parse;

    fn set_iterable_proof(src: &str) -> (SetIterableShapes, Span) {
        let module = parse(src);
        let body = top_level_item_views(&module.body);
        let shadowed = compute_shadowed_globals(&body);
        let flow = ChunkValueFlow::analyze_with_candidates(
            &body,
            &crate::facts::abstract_domain::heap::tracked_allocation_roots(&body),
        );
        let plain_data =
            crate::facts::abstract_domain::heap::ChunkHeap::analyze(&body, &shadowed, &flow, None)
                .plain_data_bindings()
                .bindings;
        let proof = SetIterableShapes::analyze(
            &body,
            &shadowed,
            &flow,
            &plain_data,
            DynamicScopeFact::analyze(&body),
        );
        struct NewSpans(Vec<Span>);
        impl Visit for NewSpans {
            fn visit_new_expr(&mut self, node: &NewExpr) {
                if matches!(node.callee.unwrap_parens(), Expr::Ident(ident) if ident.sym.as_ref() == "Set")
                {
                    self.0.push(node.span);
                }
                node.visit_children_with(self);
            }
        }
        let mut spans = NewSpans(Vec::new());
        module.visit_with(&mut spans);
        assert_eq!(spans.0.len(), 1, "fixture needs one constructor");
        (proof, spans.0[0])
    }

    fn proves_only_new(src: &str) -> bool {
        let (proof, span) = set_iterable_proof(src);
        proof.constructor_starts.contains(&span.lo.0)
    }

    #[test]
    fn static_member_proof_requires_unchanged_own_primitive_value() {
        assert!(proves_only_new(
            "const lookup = { key: 'token', other: 7 }, unique = new Set([lookup.key]);"
        ));
        assert!(!proves_only_new(
            "const lookup = { key: () => later }, unique = new Set([lookup.key]);"
        ));
        assert!(!proves_only_new(
            "const lookup = { key: 'token', key: () => later }, unique = new Set([lookup.key]);"
        ));
        assert!(!proves_only_new(
            "const lookup = { get key() { return 'token'; } }, unique = new Set([lookup.key]);"
        ));
        assert!(!proves_only_new(
            "const lookup = new Proxy({ key: 'token' }, {}), unique = new Set([lookup.key]);"
        ));
        assert!(!proves_only_new(
            "const lookup = { key: 'token' }, alias = lookup; alias.key = () => later; const unique = new Set([lookup.key]);"
        ));
        assert!(!proves_only_new(
            "const lookup = { key: 'token' }; function make(lookup) { return new Set([lookup.key]); }"
        ));
    }

    #[test]
    fn array_alias_invalidation_reaches_the_whole_chain() {
        assert!(proves_only_new(
            "const values = ['a'], alias = values, tail = alias; const unique = new Set(tail);"
        ));
        assert!(!proves_only_new(
            "const values = ['a'], alias = values, tail = alias; values.push('b'); const unique = new Set(tail);"
        ));
        assert!(!proves_only_new(
            "const values = ['a'], alias = values, tail = alias; tail.push('b'); const unique = new Set(values);"
        ));
    }

    #[test]
    fn checked_const_array_aliases_use_the_shared_source_projection() {
        let (proof, span) =
            set_iterable_proof("const input = ['a']; const result = new Set(input);");
        assert!(proof.borrowed_const_array_operand(span).is_some());

        let (proof, span) = set_iterable_proof(
            "const input = ['a']; const alias = input; const tail = alias; const result = new Set(tail);",
        );
        assert!(matches!(
            proof.borrowed_const_array_operand(span),
            Some(NativeIterableOperandOrigin::BorrowedConstArray {
                source_binding,
                allocation_owner: SourceUnitOrdinal(0),
                ..
            }) if source_binding.0.as_ref() == "tail"
        ));
        assert!(proof.constructor_starts.contains(&span.lo.0));

        for source in [
            "let input = ['a']; input = ['b']; const result = new Set(input);",
            "const input = ['a']; input.push('b'); const result = new Set(input);",
            "const input = ['a']; const result = new Set(input); input.push('b');",
            "const input = ['a']; Object.defineProperty(input, Symbol.iterator, { get() { return function* () {}; } }); const result = new Set(input);",
            "function make() { const input = ['a']; return new Set(input); } const result = make();",
            "const input = ['a']; const alias = input; const tail = alias; input.push('b'); const result = new Set(tail);",
            "const input = ['a']; const alias = input; const tail = alias; const result = new Set(tail); tail.push('b');",
            "const input = ['a']; const alias = input; const tail = alias; consume(tail); const result = new Set(input);",
            "const input = ['a']; const alias = input; const tail = alias; Object.defineProperty(tail, Symbol.iterator, { get() { return function* () {}; } }); const result = new Set(tail);",
        ] {
            let (proof, span) = set_iterable_proof(source);
            assert!(
                proof.borrowed_const_array_operand(span).is_none(),
                "unexpected borrowed-root certificate for {source}"
            );
            assert!(
                !proof.constructor_starts.contains(&span.lo.0),
                "the rejected borrowed root kept its old admission for {source}"
            );
        }
    }
}
