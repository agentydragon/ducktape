//! Nonterminal receipts from operations executed by the ordered local kernel.

use super::completion::Completion;
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::reports::schema::BindingEffectAuthorityReport;
use analysis_codec::{ContextOrdinals, SavedBinding};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeMap;
use swc_common::Span;
use swc_ecma_ast::Id;

#[derive(Debug, Clone, Default)]
pub(super) enum DomainCoverage {
    #[default]
    Unknown,
    Covered(Box<ProofDependencies>),
}

impl DomainCoverage {
    fn compose(&mut self, next: &Self) {
        match (&mut *self, next) {
            (Self::Covered(current), Self::Covered(next)) => current.extend(next),
            _ => *self = Self::Unknown,
        }
    }
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
    Native {
        intrinsic: NativeIntrinsic,
        property: Option<String>,
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
    pub(super) dependencies: Box<ProofDependencies>,
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
    pub(crate) fn snapshot(
        &self,
        ordinals: &ContextOrdinals,
    ) -> Result<SavedBindingEffectCoverage> {
        Ok(match self {
            Self::Unclassified => SavedBindingEffectCoverage::Unclassified,
            Self::LegacyExecution => SavedBindingEffectCoverage::LegacyExecution,
            Self::OrderedRootInitializer { identity, receipt } => {
                SavedBindingEffectCoverage::OrderedRootInitializer {
                    identity: SavedRootInitializerIdentity::snapshot(identity, ordinals)?,
                    receipt: receipt
                        .as_deref()
                        .map(|receipt| {
                            SavedLexicalBindingEffectReceipt::snapshot(receipt, ordinals)
                        })
                        .transpose()?
                        .map(Box::new),
                }
            }
        })
    }

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

/// Process-independent binding-effect authority retained by staged analysis.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum SavedBindingEffectCoverage {
    Unclassified,
    LegacyExecution,
    OrderedRootInitializer {
        identity: SavedRootInitializerIdentity,
        receipt: Option<Box<SavedLexicalBindingEffectReceipt>>,
    },
}

impl SavedBindingEffectCoverage {
    pub(crate) fn restore(self, ordinals: &ContextOrdinals) -> Result<BindingEffectCoverage> {
        Ok(match self {
            Self::Unclassified => BindingEffectCoverage::Unclassified,
            Self::LegacyExecution => BindingEffectCoverage::LegacyExecution,
            Self::OrderedRootInitializer { identity, receipt } => {
                BindingEffectCoverage::OrderedRootInitializer {
                    identity: identity.restore(ordinals)?,
                    receipt: receipt
                        .map(|receipt| receipt.restore(ordinals))
                        .transpose()?
                        .map(Box::new),
                }
            }
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SavedRootInitializerIdentity {
    owner: SourceUnitOrdinal,
    binding: SavedBinding,
    initializer_span: Option<Span>,
}

impl SavedRootInitializerIdentity {
    fn snapshot(identity: &RootInitializerIdentity, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            owner: identity.owner,
            binding: ordinals.snapshot_binding(&identity.binding)?,
            initializer_span: identity.initializer_span,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<RootInitializerIdentity> {
        Ok(RootInitializerIdentity {
            owner: self.owner,
            binding: ordinals.restore_binding(self.binding)?,
            initializer_span: self.initializer_span,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SavedLexicalBindingEffectReceipt {
    identity: SavedRootInitializerIdentity,
    read_dependencies: ProofDependencies,
    rebind_dependencies: ProofDependencies,
    calls: Vec<LocalCallReceipt>,
}

impl SavedLexicalBindingEffectReceipt {
    fn snapshot(receipt: &LexicalBindingEffectReceipt, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            identity: SavedRootInitializerIdentity::snapshot(&receipt.identity, ordinals)?,
            read_dependencies: receipt.read_dependencies.clone(),
            rebind_dependencies: receipt.rebind_dependencies.clone(),
            calls: receipt.calls.clone(),
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<LexicalBindingEffectReceipt> {
        Ok(LexicalBindingEffectReceipt {
            identity: self.identity.restore(ordinals)?,
            read_dependencies: self.read_dependencies,
            rebind_dependencies: self.rebind_dependencies,
            calls: self.calls,
        })
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
                        LocalBodyIdentity::Native {
                            intrinsic,
                            property,
                        } => LexicalBindingEffectBody::Native {
                            intrinsic: *intrinsic,
                            property: property.clone(),
                        },
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
                        LocalBodyIdentity::Native {
                            intrinsic,
                            property,
                        } => LexicalBindingEffectBody::Native {
                            intrinsic: *intrinsic,
                            property: property.clone(),
                        },
                    },
                    dependencies: call.dependencies.as_ref().clone(),
                })
                .collect(),
        }
    }
}

/// This describes only the operations of one execution. It never identifies
/// or certifies a whole source owner, and may travel through invocation memos.
#[must_use]
#[derive(Debug, Clone, Default)]
pub(super) struct EffectCoverageFragment {
    initialization: Option<Box<InitializationEffectCoverage>>,
    lexical: Option<Box<LexicalEffectCoverage>>,
    completion: Completion<(), ()>,
}

/// The three existing initialization domains remain independent, but are
/// grouped behind one pointer so every recursive fragment does not carry
/// three inline proof-dependency payloads.
#[derive(Debug, Clone, Default)]
struct InitializationEffectCoverage {
    global_properties: DomainCoverage,
    general_heap: DomainCoverage,
    host_effects: DomainCoverage,
}

/// Kept behind one pointer because `ProofDependencies` contains several
/// inline set headers. This data is absent on the conservative default path,
/// which is common in deep ordinary call execution.
#[derive(Debug, Clone)]
struct LexicalEffectCoverage {
    reads: DomainCoverage,
    rebinds: DomainCoverage,
    calls: BTreeMap<LocalCallKey, LocalCallReceipt>,
}

impl EffectCoverageFragment {
    /// Explicit receipt for an operation that touches none of these domains,
    /// such as GetBindingValue of an initialized module declarative cell.
    /// Source identity and actual binding effects remain separate evidence.
    pub(super) fn no_effects() -> Self {
        Self {
            initialization: Some(Box::new(InitializationEffectCoverage {
                global_properties: DomainCoverage::Covered(Box::default()),
                general_heap: DomainCoverage::Covered(Box::default()),
                host_effects: DomainCoverage::Covered(Box::default()),
            })),
            lexical: Some(Box::new(LexicalEffectCoverage {
                reads: DomainCoverage::Covered(Box::default()),
                rebinds: DomainCoverage::Covered(Box::default()),
                calls: BTreeMap::new(),
            })),
            completion: Completion::Normal(()),
        }
    }

    /// Checked native body with no application callback, accessor, host
    /// operation or exterior write. Callee and operand evaluation remain with
    /// the caller; this receipt grants no lexical binding-effect authority.
    pub(super) fn native_operation(dependencies: &ProofDependencies) -> Self {
        Self {
            initialization: Some(Box::new(InitializationEffectCoverage {
                global_properties: DomainCoverage::Covered(Box::new(dependencies.clone())),
                general_heap: DomainCoverage::Covered(Box::new(dependencies.clone())),
                host_effects: DomainCoverage::Covered(Box::new(dependencies.clone())),
            })),
            // Native operands are outside the bounded lexical migration.
            lexical: None,
            completion: Completion::Normal(()),
        }
    }

    /// Add the audited nonlexical domains of one selected initializer call.
    /// This author input does not change completion or lexical coverage.
    pub(super) fn audited_initializer_call_domains(
        mut self,
        dependencies: &ProofDependencies,
    ) -> Self {
        let merged = |prior: Option<&DomainCoverage>| {
            let mut covered = dependencies.clone();
            if let Some(DomainCoverage::Covered(prior)) = prior {
                covered.extend(prior);
            }
            DomainCoverage::Covered(Box::new(covered))
        };
        let prior = self.initialization.as_deref();
        self.initialization = Some(Box::new(InitializationEffectCoverage {
            global_properties: merged(prior.map(|coverage| &coverage.global_properties)),
            general_heap: merged(prior.map(|coverage| &coverage.general_heap)),
            host_effects: merged(prior.map(|coverage| &coverage.host_effects)),
        }));
        self
    }

    /// The separate exact initializer-purity assertion excludes throws. The
    /// caller also checks its actual normal result and execution path; an
    /// already observed abrupt completion cannot be replaced by this proof.
    pub(super) fn normal_completion_from_pure_initializer(&mut self) {
        if !self.completion.has_no_normal_path() {
            self.completion = Completion::Normal(());
        }
    }

    /// Actual ordinary native iteration over a proved primitive source.
    /// Its protocol/contents reads and fresh internal allocation are modeled;
    /// this is body coverage, not completion of operand evaluation. The
    /// intact protocol reads and rebinds no lexical cell, under the same
    /// intrinsic premises; the operand's own reads are the caller's.
    pub(super) fn native_iterable_protocol(dependencies: &ProofDependencies) -> Self {
        let mut fragment = Self::native_operation(dependencies);
        fragment.lexical = Some(Box::new(LexicalEffectCoverage {
            reads: DomainCoverage::Covered(Box::new(dependencies.clone())),
            rebinds: DomainCoverage::Covered(Box::new(dependencies.clone())),
            calls: BTreeMap::new(),
        }));
        fragment
    }

    /// A top-level module-cell read has no global/heap/host effect, and its
    /// lexical read/rebind behavior is accounted for by the caller.
    pub(super) fn initialized_module_read() -> Self {
        let mut fragment = Self::no_effects();
        fragment
            .lexical
            .as_mut()
            .expect("no-effects fragment carries known lexical domains")
            .reads = DomainCoverage::Covered(Box::default());
        fragment
    }

    /// An executed lexical operation whose global, heap, and host effects
    /// remain outside this receipt family. Keeping those domains Unknown is
    /// deliberate: this proof can seal binding-effect coverage only.
    pub(super) fn lexical_operation() -> Self {
        Self {
            lexical: Some(Box::new(LexicalEffectCoverage {
                reads: DomainCoverage::Covered(Box::default()),
                rebinds: DomainCoverage::Covered(Box::default()),
                calls: BTreeMap::new(),
            })),
            completion: Completion::Normal(()),
            ..Self::default()
        }
    }

    /// Proof prerequisites for a successfully consumed own-data selection.
    /// Unknown lexical coverage never becomes positive through dependencies.
    pub(super) fn lexical_selection_dependencies(&self) -> Option<Box<ProofDependencies>> {
        if !self.completion.is_exact_normal() {
            return None;
        }
        let lexical = self.lexical.as_ref()?;
        let DomainCoverage::Covered(reads) = &lexical.reads else {
            return None;
        };
        let DomainCoverage::Covered(rebinds) = &lexical.rebinds else {
            return None;
        };
        let mut dependencies = Box::<ProofDependencies>::default();
        dependencies.extend(reads);
        dependencies.extend(rebinds);
        Some(dependencies)
    }

    pub(super) fn compose(&mut self, next: &Self) {
        match (&mut self.initialization, &next.initialization) {
            (Some(current), Some(next)) => {
                current.global_properties.compose(&next.global_properties);
                current.general_heap.compose(&next.general_heap);
                current.host_effects.compose(&next.host_effects);
            }
            _ => self.initialization = None,
        }
        match (&mut self.lexical, &next.lexical) {
            (Some(current), Some(next)) => {
                current.reads.compose(&next.reads);
                current.rebinds.compose(&next.rebinds);
                for (key, next_call) in &next.calls {
                    current
                        .calls
                        .entry(key.clone())
                        .and_modify(|call| call.dependencies.extend(&next_call.dependencies))
                        .or_insert_with(|| next_call.clone());
                }
            }
            _ => self.lexical = None,
        }
        self.completion = std::mem::take(&mut self.completion).then(next.completion.clone());
    }

    /// Prerequisites of selecting an already covered operation. Conditions
    /// never turn an unknown domain into a covered one.
    pub(super) fn depend_on(&mut self, dependencies: &ProofDependencies) {
        if let Some(initialization) = &mut self.initialization {
            for domain in [
                &mut initialization.global_properties,
                &mut initialization.general_heap,
                &mut initialization.host_effects,
            ] {
                if let DomainCoverage::Covered(current) = domain {
                    current.extend(dependencies);
                }
            }
        }
        if let Some(lexical) = &mut self.lexical {
            for domain in [&mut lexical.reads, &mut lexical.rebinds] {
                if let DomainCoverage::Covered(current) = domain {
                    current.extend(dependencies);
                }
            }
            for call in lexical.calls.values_mut() {
                call.dependencies.extend(dependencies);
            }
        }
    }

    /// Add one selected call edge without expanding child paths.
    pub(super) fn selected_local_call(&mut self, call: LocalCallReceipt) {
        if let Some(lexical) = &mut self.lexical {
            lexical
                .calls
                .entry(local_call_key(&call))
                .and_modify(|existing| existing.dependencies.extend(&call.dependencies))
                .or_insert(call);
        }
    }

    pub(super) fn lexical_effect_dependencies(
        &self,
    ) -> Option<(ProofDependencies, ProofDependencies)> {
        if !self.completion.is_exact_normal() {
            return None;
        }
        let lexical = self.lexical.as_ref()?;
        let DomainCoverage::Covered(read_dependencies) = &lexical.reads else {
            return None;
        };
        let DomainCoverage::Covered(rebind_dependencies) = &lexical.rebinds else {
            return None;
        };
        Some((
            read_dependencies.as_ref().clone(),
            rebind_dependencies.as_ref().clone(),
        ))
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
            .values()
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
        for call in lexical.calls.values() {
            adjacency
                .entry(local_body_key(&call.caller))
                .or_default()
                .insert(local_body_key(&call.target));
        }
        let root_key = local_body_key(&root_body);
        let mut reachable_bodies = std::collections::BTreeSet::from([root_key.clone()]);
        let mut pending = vec![root_key];
        while let Some(body) = pending.pop() {
            if let Some(targets) = adjacency.get(&body) {
                for target in targets {
                    if reachable_bodies.insert(target.clone()) {
                        pending.push(target.clone());
                    }
                }
            }
        }
        if lexical
            .calls
            .values()
            .any(|call| !reachable_bodies.contains(&local_body_key(&call.caller)))
        {
            return None;
        }
        Some(LexicalBindingEffectReceipt {
            identity,
            read_dependencies,
            rebind_dependencies,
            calls: lexical.calls.values().cloned().collect(),
        })
    }

    pub(super) fn abrupt(&mut self) {
        self.completion = Completion::Abrupt(());
    }

    pub(super) fn uncertain_completion(&mut self) {
        self.completion = Completion::default();
    }

    pub(super) fn normal_dependencies(&self) -> Option<ProofDependencies> {
        if !self.completion.is_exact_normal() {
            return None;
        }
        let initialization = self.initialization.as_ref()?;
        let mut dependencies = ProofDependencies::default();
        for domain in [
            &initialization.global_properties,
            &initialization.general_heap,
            &initialization.host_effects,
        ] {
            let DomainCoverage::Covered(domain_dependencies) = domain else {
                return None;
            };
            dependencies.extend(domain_dependencies);
        }
        Some(dependencies)
    }
}

/// Compact ordered identity for the edge map and iterative connectivity check.
/// Spans are represented by their exact byte coordinates because SWC `Span`
/// itself does not implement `Ord`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum LocalBodyKey {
    RootInitializer {
        owner: usize,
        lo: u32,
        hi: u32,
    },
    Callable {
        key: u32,
        owner: usize,
    },
    Native {
        intrinsic: NativeIntrinsic,
        property: Option<String>,
    },
}

/// Exact identity for a flat local call edge. Span coordinates are used
/// because SWC `Span` itself does not implement `Ord`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct LocalCallKey {
    caller: LocalBodyKey,
    call_lo: u32,
    call_hi: u32,
    target: LocalBodyKey,
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
        LocalBodyIdentity::Native {
            intrinsic,
            property,
        } => LocalBodyKey::Native {
            intrinsic: *intrinsic,
            property: property.clone(),
        },
    }
}

fn local_call_key(call: &LocalCallReceipt) -> LocalCallKey {
    LocalCallKey {
        caller: local_body_key(&call.caller),
        call_lo: call.call_span.lo.0,
        call_hi: call.call_span.hi.0,
        target: local_body_key(&call.target),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::{ProgramPremiseKey, ValueInputRef};
    use swc_common::BytePos;

    fn test_callable(key: u32, owner: usize) -> LocalBodyIdentity {
        LocalBodyIdentity::Callable {
            key,
            owner: SourceUnitOrdinal(owner),
        }
    }

    fn test_call(
        caller: LocalBodyIdentity,
        lo: u32,
        hi: u32,
        target: LocalBodyIdentity,
        dependencies: ProofDependencies,
    ) -> LocalCallReceipt {
        LocalCallReceipt {
            caller,
            call_span: Span::new(BytePos(lo), BytePos(hi)),
            target,
            dependencies: Box::new(dependencies),
        }
    }

    fn captured_value_input(input_index: usize) -> ProofDependencies {
        ProofDependencies {
            value_inputs: std::collections::BTreeSet::from([ValueInputRef {
                defining_chunk: "static/provider".to_string(),
                input_index,
            }]),
            ..ProofDependencies::default()
        }
    }

    #[test]
    fn default_binding_effect_authority_is_unknown() {
        assert_eq!(
            BindingEffectCoverage::default().authority(true, false),
            BindingEffectAuthority::Unclassified,
            "programmatic default remains fail-closed"
        );
    }

    #[test]
    fn domains_compose_independently_and_unknown_is_absorbing() {
        let mut first = EffectCoverageFragment::no_effects();
        let initialization = first.initialization.as_mut().unwrap();
        let DomainCoverage::Covered(dependencies) = &mut initialization.global_properties else {
            unreachable!();
        };
        dependencies
            .program_premises
            .insert(ProgramPremiseKey::IntrinsicsNeverWritten);
        let mut second = EffectCoverageFragment::no_effects();
        second.initialization.as_mut().unwrap().general_heap = DomainCoverage::Unknown;
        first.compose(&second);
        assert!(
            matches!(&first.initialization.as_ref().unwrap().global_properties, DomainCoverage::Covered(dependencies)
            if dependencies.program_premises.contains(&ProgramPremiseKey::IntrinsicsNeverWritten))
        );
        assert!(matches!(
            &first.initialization.as_ref().unwrap().general_heap,
            DomainCoverage::Unknown
        ));
        assert!(matches!(
            &first.initialization.as_ref().unwrap().host_effects,
            DomainCoverage::Covered(_)
        ));
        assert!(first.normal_dependencies().is_none());
        first.compose(&EffectCoverageFragment::no_effects());
        assert!(matches!(
            &first.initialization.as_ref().unwrap().general_heap,
            DomainCoverage::Unknown
        ));
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
        assert!(unknown.initialization.is_none());
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

    #[test]
    fn repeated_diamond_composition_unions_call_dependencies_and_unknown_absorbs() {
        let root = LocalBodyIdentity::RootInitializer {
            owner: SourceUnitOrdinal(0),
            initializer_span: Span::new(BytePos(1), BytePos(100)),
        };
        let branch = test_callable(1, 0);
        let leaf = test_callable(2, 0);

        // Two root call sites reach the same branch body. Each execution then
        // contributes the same branch-to-leaf source edge under distinct
        // captured-input prerequisites.
        let mut first_path = EffectCoverageFragment::no_effects();
        first_path.selected_local_call(test_call(
            root.clone(),
            10,
            15,
            branch.clone(),
            captured_value_input(1),
        ));
        first_path.selected_local_call(test_call(
            branch.clone(),
            40,
            45,
            leaf.clone(),
            captured_value_input(1),
        ));

        let mut second_path = EffectCoverageFragment::no_effects();
        second_path.selected_local_call(test_call(
            root,
            20,
            25,
            branch.clone(),
            captured_value_input(2),
        ));
        second_path.selected_local_call(test_call(
            branch.clone(),
            40,
            45,
            leaf,
            captured_value_input(2),
        ));

        let mut composed = first_path.clone();
        composed.compose(&second_path);
        composed.compose(&first_path);
        let calls = &composed.lexical.as_ref().unwrap().calls;
        assert_eq!(calls.len(), 3, "each exact source edge is retained once");
        let branch_to_leaf = calls
            .values()
            .find(|call| call.caller == branch && call.call_span.lo.0 == 40)
            .expect("shared branch-to-leaf edge");
        assert_eq!(
            branch_to_leaf.dependencies.value_inputs,
            std::collections::BTreeSet::from([
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 1,
                },
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 2,
                },
            ]),
            "prerequisites from both contexts are retained"
        );

        composed.compose(&EffectCoverageFragment::default());
        assert!(
            composed.lexical.is_none(),
            "unknown lexical coverage absorbs"
        );
        assert!(composed.lexical_effect_dependencies().is_none());
    }

    #[test]
    fn call_edge_identity_distinguishes_caller_span_and_target() {
        let caller = test_callable(1, 3);
        let target = test_callable(5, 6);
        let key = local_call_key(&test_call(
            caller.clone(),
            10,
            15,
            target.clone(),
            ProofDependencies::default(),
        ));

        for distinct in [
            test_call(
                caller.clone(),
                11,
                15,
                target.clone(),
                ProofDependencies::default(),
            ),
            test_call(
                test_callable(1, 4),
                10,
                15,
                target.clone(),
                ProofDependencies::default(),
            ),
            test_call(
                test_callable(2, 3),
                10,
                15,
                target.clone(),
                ProofDependencies::default(),
            ),
            test_call(
                caller.clone(),
                10,
                15,
                test_callable(5, 7),
                ProofDependencies::default(),
            ),
            test_call(
                caller,
                10,
                15,
                test_callable(8, 6),
                ProofDependencies::default(),
            ),
        ] {
            assert_ne!(
                key,
                local_call_key(&distinct),
                "a differing caller identity, span, or target keeps a separate edge"
            );
        }
    }
}
