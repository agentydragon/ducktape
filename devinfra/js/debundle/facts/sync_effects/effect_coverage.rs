//! Nonterminal receipts from operations executed by the ordered local kernel.

use crate::analysis_inputs::ProofDependencies;
use crate::reports::schema::BindingEffectAuthorityReport;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;
use swc_ecma_ast::Id;

#[derive(Debug, Clone, Default)]
pub(super) enum DomainCoverage {
    #[default]
    Unknown,
    Covered(ProofDependencies),
}

impl DomainCoverage {
    fn compose(&mut self, next: &Self) {
        match (&mut *self, next) {
            (Self::Covered(current), Self::Covered(next)) => current.extend(next),
            _ => *self = Self::Unknown,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(super) enum Completion {
    #[default]
    Unknown,
    Normal,
    Abrupt,
}

/// Exact identity for the one root initializer whose ordered execution may
/// seal lexical binding-effect coverage.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct RootInitializerIdentity {
    pub(super) owner: SourceUnitOrdinal,
    pub(super) binding: Id,
    pub(super) initializer_span: Option<Span>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) enum LocalBodyIdentity {
    RootInitializer {
        owner: SourceUnitOrdinal,
        initializer_span: Span,
    },
    Callable {
        key: u32,
        owner: SourceUnitOrdinal,
    },
}

/// One selected direct local invocation. The flat edge set avoids expanding
/// every root-to-leaf path; each edge retains prerequisites merged from every
/// invocation context that selected it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct LocalCallReceipt {
    pub(super) caller: LocalBodyIdentity,
    pub(super) call_span: Span,
    pub(super) target: LocalBodyIdentity,
    pub(super) dependencies: ProofDependencies,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct LexicalBindingEffectReceipt {
    pub(super) identity: RootInitializerIdentity,
    pub(super) read_dependencies: ProofDependencies,
    pub(super) rebind_dependencies: ProofDependencies,
    pub(super) calls: Vec<LocalCallReceipt>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum BindingEffectCoverage {
    #[default]
    Unclassified,
    LegacyExecution,
    OrderedRootInitializer {
        identity: RootInitializerIdentity,
        receipt: Option<Box<LexicalBindingEffectReceipt>>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum BindingEffectAuthority {
    Unclassified,
    LegacyExecution { complete: bool },
    AuthorSourceOnly,
    OrderedRootInitializerUncovered,
    OperationReceipt,
}

impl BindingEffectCoverage {
    pub(crate) fn authority(
        &self,
        complete: bool,
        author_source_only: bool,
    ) -> BindingEffectAuthority {
        if author_source_only {
            return BindingEffectAuthority::AuthorSourceOnly;
        }
        match self {
            Self::Unclassified => BindingEffectAuthority::Unclassified,
            Self::LegacyExecution => BindingEffectAuthority::LegacyExecution { complete },
            Self::OrderedRootInitializer {
                receipt: Some(_), ..
            } => BindingEffectAuthority::OperationReceipt,
            Self::OrderedRootInitializer { receipt: None, .. } => {
                BindingEffectAuthority::OrderedRootInitializerUncovered
            }
        }
    }

    pub(crate) fn authority_report(
        &self,
        complete: bool,
        author_source_only: bool,
    ) -> BindingEffectAuthorityReport {
        match self.authority(complete, author_source_only) {
            BindingEffectAuthority::Unclassified => BindingEffectAuthorityReport::Unclassified,
            BindingEffectAuthority::AuthorSourceOnly => {
                BindingEffectAuthorityReport::AuthorSourceOnly
            }
            BindingEffectAuthority::LegacyExecution { complete } => {
                BindingEffectAuthorityReport::LegacyExecution { complete }
            }
            BindingEffectAuthority::OperationReceipt => {
                let Self::OrderedRootInitializer {
                    receipt: Some(receipt),
                    ..
                } = self
                else {
                    unreachable!("typed authority comes from this receipt");
                };
                BindingEffectAuthorityReport::OperationReceipt {
                    receipt: Box::new(receipt.as_ref().to_report()),
                }
            }
            BindingEffectAuthority::OrderedRootInitializerUncovered => {
                let Self::OrderedRootInitializer { identity, .. } = self else {
                    unreachable!("typed authority comes from this root attempt");
                };
                BindingEffectAuthorityReport::OrderedRootInitializerUncovered {
                    owner: identity.owner,
                    binding: identity.binding.0.to_string(),
                    initializer_span: identity.initializer_span,
                }
            }
        }
    }
}

impl LexicalBindingEffectReceipt {
    pub(super) fn to_report(&self) -> crate::reports::schema::LexicalBindingEffectReceipt {
        use crate::reports::schema::{
            LexicalBindingEffectBody, LexicalBindingEffectCall, LexicalBindingEffectReceipt,
        };

        LexicalBindingEffectReceipt {
            owner: self.identity.owner,
            binding: self.identity.binding.0.to_string(),
            initializer_span: self.identity.initializer_span,
            read_dependencies: self.read_dependencies.clone(),
            rebind_dependencies: self.rebind_dependencies.clone(),
            calls: self
                .calls
                .iter()
                .map(|call| LexicalBindingEffectCall {
                    caller: match &call.caller {
                        LocalBodyIdentity::RootInitializer {
                            owner,
                            initializer_span,
                        } => LexicalBindingEffectBody::RootInitializer {
                            owner: *owner,
                            initializer_span: *initializer_span,
                        },
                        LocalBodyIdentity::Callable { key, owner } => {
                            LexicalBindingEffectBody::Callable {
                                key: *key,
                                owner: *owner,
                            }
                        }
                    },
                    call_span: call.call_span,
                    target: match &call.target {
                        LocalBodyIdentity::RootInitializer {
                            owner,
                            initializer_span,
                        } => LexicalBindingEffectBody::RootInitializer {
                            owner: *owner,
                            initializer_span: *initializer_span,
                        },
                        LocalBodyIdentity::Callable { key, owner } => {
                            LexicalBindingEffectBody::Callable {
                                key: *key,
                                owner: *owner,
                            }
                        }
                    },
                    dependencies: call.dependencies.clone(),
                })
                .collect(),
        }
    }
}

/// This describes only the operations of one execution. It never identifies
/// or certifies a whole source owner, and may travel through invocation memos.
#[derive(Debug, Clone, Default)]
pub(super) struct EffectCoverageFragment {
    global_properties: DomainCoverage,
    general_heap: DomainCoverage,
    host_effects: DomainCoverage,
    lexical: Option<Box<LexicalEffectCoverage>>,
    completion: Completion,
}

/// Kept behind one pointer because `ProofDependencies` contains several
/// inline set headers. This data is absent on the conservative default path,
/// which is common in deep ordinary call execution.
#[derive(Debug, Clone)]
struct LexicalEffectCoverage {
    reads: DomainCoverage,
    rebinds: DomainCoverage,
    calls: Vec<LocalCallReceipt>,
}

impl EffectCoverageFragment {
    /// Explicit receipt for an operation that touches none of these domains,
    /// such as GetBindingValue of an initialized module declarative cell.
    /// Source identity and actual binding effects remain separate evidence.
    pub(super) fn no_effects() -> Self {
        Self {
            global_properties: DomainCoverage::Covered(ProofDependencies::default()),
            general_heap: DomainCoverage::Covered(ProofDependencies::default()),
            host_effects: DomainCoverage::Covered(ProofDependencies::default()),
            lexical: Some(Box::new(LexicalEffectCoverage {
                reads: DomainCoverage::Covered(ProofDependencies::default()),
                rebinds: DomainCoverage::Covered(ProofDependencies::default()),
                calls: Vec::new(),
            })),
            completion: Completion::Normal,
        }
    }

    /// Actual ordinary native iteration over a proved primitive source.
    /// Its protocol/contents reads and fresh internal allocation are modeled;
    /// it runs no application callback, accessor, host operation or exterior
    /// write. This is body coverage, not completion of operand evaluation.
    pub(super) fn native_iterable_protocol(dependencies: &ProofDependencies) -> Self {
        Self {
            global_properties: DomainCoverage::Covered(dependencies.clone()),
            general_heap: DomainCoverage::Covered(dependencies.clone()),
            host_effects: DomainCoverage::Covered(dependencies.clone()),
            // Native operand paths are outside the bounded lexical migration.
            // Preserve their initialization coverage without granting authority
            // to suppress binding fallback.
            lexical: None,
            completion: Completion::Normal,
        }
    }

    /// A top-level module-cell read has no global/heap/host effect, and its
    /// lexical read/rebind behavior is accounted for by the caller.
    pub(super) fn initialized_module_read() -> Self {
        let mut fragment = Self::no_effects();
        fragment
            .lexical
            .as_mut()
            .expect("no-effects fragment carries known lexical domains")
            .reads = DomainCoverage::Covered(ProofDependencies::default());
        fragment
    }

    pub(super) fn compose(&mut self, next: &Self) {
        self.global_properties.compose(&next.global_properties);
        self.general_heap.compose(&next.general_heap);
        self.host_effects.compose(&next.host_effects);
        match (&mut self.lexical, &next.lexical) {
            (Some(current), Some(next)) => {
                current.reads.compose(&next.reads);
                current.rebinds.compose(&next.rebinds);
                current.calls.extend(next.calls.iter().cloned());
            }
            _ => self.lexical = None,
        }
        self.completion = match (self.completion, next.completion) {
            (Completion::Unknown, _) | (_, Completion::Unknown) => Completion::Unknown,
            (Completion::Abrupt, _) | (_, Completion::Abrupt) => Completion::Abrupt,
            (Completion::Normal, Completion::Normal) => Completion::Normal,
        };
    }

    /// Prerequisites of selecting an already covered operation. Conditions
    /// never turn an unknown domain into a covered one.
    pub(super) fn depend_on(&mut self, dependencies: &ProofDependencies) {
        for domain in [
            &mut self.global_properties,
            &mut self.general_heap,
            &mut self.host_effects,
        ] {
            if let DomainCoverage::Covered(current) = domain {
                current.extend(dependencies);
            }
        }
        if let Some(lexical) = &mut self.lexical {
            for domain in [&mut lexical.reads, &mut lexical.rebinds] {
                if let DomainCoverage::Covered(current) = domain {
                    current.extend(dependencies);
                }
            }
            for call in &mut lexical.calls {
                call.dependencies.extend(dependencies);
            }
        }
    }

    /// Add one selected call edge without expanding child paths.
    pub(super) fn selected_local_call(&mut self, call: LocalCallReceipt) {
        if let Some(lexical) = &mut self.lexical {
            lexical.calls.push(call);
        }
    }

    pub(super) fn lexical_effect_dependencies(
        &self,
    ) -> Option<(ProofDependencies, ProofDependencies)> {
        if self.completion != Completion::Normal {
            return None;
        }
        let lexical = self.lexical.as_ref()?;
        let DomainCoverage::Covered(read_dependencies) = &lexical.reads else {
            return None;
        };
        let DomainCoverage::Covered(rebind_dependencies) = &lexical.rebinds else {
            return None;
        };
        Some((read_dependencies.clone(), rebind_dependencies.clone()))
    }

    pub(super) fn seal_lexical_binding_effects(
        &self,
        identity: RootInitializerIdentity,
        expected_root_calls: &std::collections::BTreeSet<(u32, u32)>,
    ) -> Option<LexicalBindingEffectReceipt> {
        let (read_dependencies, rebind_dependencies) = self.lexical_effect_dependencies()?;
        let initializer_span = identity.initializer_span?;
        let root_body = LocalBodyIdentity::RootInitializer {
            owner: identity.owner,
            initializer_span,
        };
        let lexical = self.lexical.as_ref()?;
        let observed_root_calls: std::collections::BTreeSet<_> = lexical
            .calls
            .iter()
            .filter(|call| call.caller == root_body)
            .map(|call| (call.call_span.lo.0, call.call_span.hi.0))
            .collect();
        if &observed_root_calls != expected_root_calls {
            return None;
        }

        // A receipt can contain only edges reached while executing this root.
        // Check the flattened caller/target relation iteratively, without
        // reconstructing exponentially many invocation paths or repeatedly
        // scanning every edge for each newly reached body.
        let mut adjacency = std::collections::BTreeMap::<
            LocalBodyKey,
            std::collections::BTreeSet<LocalBodyKey>,
        >::new();
        for call in &lexical.calls {
            adjacency
                .entry(local_body_key(&call.caller))
                .or_default()
                .insert(local_body_key(&call.target));
        }
        let root_key = local_body_key(&root_body);
        let mut reachable_bodies = std::collections::BTreeSet::from([root_key]);
        let mut pending = vec![root_key];
        while let Some(body) = pending.pop() {
            if let Some(targets) = adjacency.get(&body) {
                for target in targets {
                    if reachable_bodies.insert(*target) {
                        pending.push(*target);
                    }
                }
            }
        }
        if lexical
            .calls
            .iter()
            .any(|call| !reachable_bodies.contains(&local_body_key(&call.caller)))
        {
            return None;
        }
        Some(LexicalBindingEffectReceipt {
            identity,
            read_dependencies,
            rebind_dependencies,
            calls: lexical.calls.clone(),
        })
    }

    pub(super) fn abrupt(&mut self) {
        self.completion = Completion::Abrupt;
    }

    pub(super) fn uncertain_completion(&mut self) {
        self.completion = Completion::Unknown;
    }

    pub(super) fn normal_dependencies(&self) -> Option<ProofDependencies> {
        if self.completion != Completion::Normal {
            return None;
        }
        let mut dependencies = ProofDependencies::default();
        for domain in [
            &self.global_properties,
            &self.general_heap,
            &self.host_effects,
        ] {
            let DomainCoverage::Covered(domain_dependencies) = domain else {
                return None;
            };
            dependencies.extend(domain_dependencies);
        }
        Some(dependencies)
    }
}

/// Compact ordered identity used only for the iterative connectivity check.
/// Spans are represented by their exact byte coordinates because SWC `Span`
/// itself does not implement `Ord`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum LocalBodyKey {
    RootInitializer { owner: usize, lo: u32, hi: u32 },
    Callable { key: u32, owner: usize },
}

fn local_body_key(body: &LocalBodyIdentity) -> LocalBodyKey {
    match body {
        LocalBodyIdentity::RootInitializer {
            owner,
            initializer_span,
        } => LocalBodyKey::RootInitializer {
            owner: owner.0,
            lo: initializer_span.lo.0,
            hi: initializer_span.hi.0,
        },
        LocalBodyIdentity::Callable { key, owner } => LocalBodyKey::Callable {
            key: *key,
            owner: owner.0,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ProgramPremiseKey;
    use crate::facts::statement_facts::SynchronousCalls;

    #[test]
    fn missing_staged_binding_effect_authority_is_rejected_and_default_is_unknown() {
        let mut encoded = serde_json::to_value(SynchronousCalls::default())
            .expect("default synchronous facts serialize");
        serde_json::from_value::<SynchronousCalls>(encoded.clone())
            .expect("complete synchronous facts deserialize");
        encoded
            .as_object_mut()
            .expect("synchronous facts are a struct")
            .remove("binding_effect_coverage");
        let ordinals =
            analysis_codec::ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let bytes = analysis_codec::encode(&encoded, &ordinals).expect("encode missing-field row");
        let error = analysis_codec::decode::<SynchronousCalls>(&bytes, &ordinals)
            .expect_err("staged facts without typed authority must fail to decode");
        assert!(
            format!("{error:#}").contains("binding_effect_coverage"),
            "failure should name the missing authority field, not an unrelated decode error: {error:#}"
        );
        assert_eq!(
            BindingEffectCoverage::default().authority(true, false),
            BindingEffectAuthority::Unclassified,
            "programmatic default remains fail-closed"
        );
    }

    #[test]
    fn domains_compose_independently_and_unknown_is_absorbing() {
        let mut first = EffectCoverageFragment::no_effects();
        let DomainCoverage::Covered(dependencies) = &mut first.global_properties else {
            unreachable!();
        };
        dependencies
            .program_premises
            .insert(ProgramPremiseKey::IntrinsicsNeverWritten);
        let mut second = EffectCoverageFragment::no_effects();
        second.general_heap = DomainCoverage::Unknown;
        first.compose(&second);
        assert!(
            matches!(&first.global_properties, DomainCoverage::Covered(dependencies)
            if dependencies.program_premises.contains(&ProgramPremiseKey::IntrinsicsNeverWritten))
        );
        assert!(matches!(first.general_heap, DomainCoverage::Unknown));
        assert!(matches!(first.host_effects, DomainCoverage::Covered(_)));
        assert!(first.normal_dependencies().is_none());
        first.compose(&EffectCoverageFragment::no_effects());
        assert!(matches!(first.general_heap, DomainCoverage::Unknown));
    }

    #[test]
    fn prerequisites_cannot_create_a_positive_receipt() {
        let mut dependencies = ProofDependencies::default();
        dependencies
            .program_premises
            .insert(ProgramPremiseKey::IntrinsicsNeverWritten);
        let mut unknown = EffectCoverageFragment::default();
        unknown.depend_on(&dependencies);
        assert!(unknown.normal_dependencies().is_none());
        assert!(matches!(unknown.global_properties, DomainCoverage::Unknown));
        let mut covered = EffectCoverageFragment::no_effects();
        covered.depend_on(&dependencies);
        assert_eq!(covered.normal_dependencies(), Some(dependencies));
    }

    #[test]
    fn default_and_abrupt_fragments_cannot_seal_normal_initialization() {
        assert!(
            EffectCoverageFragment::default()
                .normal_dependencies()
                .is_none()
        );
        let mut fragment = EffectCoverageFragment::no_effects();
        assert!(fragment.normal_dependencies().is_some());
        fragment.abrupt();
        fragment.compose(&EffectCoverageFragment::no_effects());
        assert!(fragment.normal_dependencies().is_none());
    }
}
