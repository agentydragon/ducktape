//! Normal, abrupt, or conservatively mixed completion for one synchronous transfer.

use super::allocation_transfer::AllocationJudgment;
use super::effect_coverage::EffectCoverageFragment;
use super::value_model::{AbstractValue, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::abstract_domain::value::HeapAddress;
use serde::{Deserialize, Serialize};

/// A completion summary. `OpaqueMayNormal` preserves a possible normal
/// payload while recording unresolved behavior separately from throws.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Completion<N, A> {
    Normal(N),
    /// A no-normal completion whose throw values are retained as a may-set,
    /// but have no single source-site witness. This must not authorize an
    /// exact skipped-call/handler receipt.
    UnknownThrow(UnknownThrowValues),
    Abrupt(A),
    OpaqueMayNormal(N),
    /// A joined transfer with both a normal continuation and one or more
    /// abrupt alternatives. Exact witnesses remain attached to their own
    /// branches; witnessless throw payloads never become source-site proof.
    Alternatives {
        normal: Option<N>,
        abrupt: Vec<AbruptAlternative<A>>,
        non_normal: NonNormalPossibilities,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AbruptAlternative<A> {
    Exact(A),
    /// `None` means an abrupt possibility is known but its payload was not
    /// represented. `Some` retains actual may-thrown values without inventing
    /// a source origin or frontier.
    Unknown(Option<UnknownThrowValues>),
}

/// Runtime throw payloads are carried by the crate-visible completion API,
/// but their analysis-private abstract values are not part of that API.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct UnknownThrowValues(pub(super) Values);

impl UnknownThrowValues {
    pub(super) fn new(values: Values) -> Self {
        Self(values)
    }
}

/// Possible behaviors not represented by an exact throw alternative. A
/// generic opaque result does not prove a throw; another path may diverge or
/// have a non-normal behavior the transfer cannot classify.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) struct NonNormalPossibilities {
    /// Typed evidence says a throw is possible, but no payload or exact
    /// source witness is represented.
    pub(crate) may_throw_without_payload: bool,
    /// A path may fail to produce any synchronous completion.
    pub(crate) may_diverge: bool,
    /// Some non-normal behavior exists but is not classified as a throw.
    /// Catch-sensitive consumers must conservatively refuse this path.
    pub(crate) unresolved: bool,
}

impl<N, A> Completion<N, A> {
    pub(crate) fn is_exact_normal(&self) -> bool {
        matches!(self, Self::Normal(_))
    }

    pub(crate) fn is_exact_abrupt(&self) -> bool {
        matches!(self, Self::Abrupt(_))
    }

    pub(crate) fn has_no_normal_path(&self) -> bool {
        matches!(self, Self::UnknownThrow(_) | Self::Abrupt(_))
            || matches!(self, Self::Alternatives { normal: None, .. })
    }

    pub(crate) fn may_normal(&self) -> Option<&N> {
        match self {
            Self::Normal(value) | Self::OpaqueMayNormal(value) => Some(value),
            Self::UnknownThrow(_) | Self::Abrupt(_) => None,
            Self::Alternatives { normal, .. } => normal.as_ref(),
        }
    }

    /// A scalar frame needs separate state only when both a normal path and
    /// an explicit abrupt alternative have represented states. Opaque
    /// non-normal possibilities remain in the ledger and keep exact
    /// completion/coverage proofs incomplete, but they do not provide a
    /// second state from which to evaluate a suffix. The normal projection
    /// may be followed conservatively while opacity stays sticky.
    pub(crate) fn requires_path_partition(&self) -> bool {
        matches!(
            self,
            Self::Alternatives {
                normal: Some(_),
                abrupt,
                ..
            } if !abrupt.is_empty()
        )
    }

    pub(crate) fn map_normal<M>(self, map: impl FnOnce(N) -> M) -> Completion<M, A> {
        match self {
            Self::Normal(value) => Completion::Normal(map(value)),
            Self::UnknownThrow(values) => Completion::UnknownThrow(values),
            Self::Abrupt(abrupt) => Completion::Abrupt(abrupt),
            Self::OpaqueMayNormal(value) => Completion::OpaqueMayNormal(map(value)),
            Self::Alternatives {
                normal,
                abrupt,
                non_normal,
            } => Completion::Alternatives {
                normal: normal.map(map),
                abrupt,
                non_normal,
            },
        }
    }

    /// Map the normal value and each exact abrupt witness independently.
    /// Payload-only abrupt alternatives remain payload-only; callers cannot
    /// accidentally attach a source witness while changing value types.
    pub(crate) fn map_parts<M, B>(
        self,
        map_normal: impl FnOnce(N) -> M,
        mut map_abrupt: impl FnMut(A) -> AbruptAlternative<B>,
    ) -> Completion<M, B> {
        let (normal, abrupt, non_normal) = self.into_alternatives();
        let abrupt = abrupt
            .into_iter()
            .map(|alternative| match alternative {
                AbruptAlternative::Exact(value) => map_abrupt(value),
                AbruptAlternative::Unknown(values) => AbruptAlternative::Unknown(values),
            })
            .collect();
        Completion::from_alternatives(normal.map(map_normal), abrupt, non_normal)
    }

    /// Expand compact completion states into normal, exact abrupt, and
    /// unresolved non-normal projections. Opaque normality may coexist with
    /// other behavior, but does not prove that a throw is among those paths.
    pub(crate) fn into_alternatives(
        self,
    ) -> (Option<N>, Vec<AbruptAlternative<A>>, NonNormalPossibilities) {
        match self {
            Self::Normal(normal) => (Some(normal), Vec::new(), NonNormalPossibilities::default()),
            Self::Abrupt(abrupt) => (
                None,
                vec![AbruptAlternative::Exact(abrupt)],
                NonNormalPossibilities::default(),
            ),
            Self::UnknownThrow(values) => (
                None,
                vec![AbruptAlternative::Unknown(Some(values))],
                NonNormalPossibilities::default(),
            ),
            Self::OpaqueMayNormal(normal) => (
                Some(normal),
                Vec::new(),
                NonNormalPossibilities {
                    may_diverge: true,
                    unresolved: true,
                    ..NonNormalPossibilities::default()
                },
            ),
            Self::Alternatives {
                normal,
                abrupt,
                non_normal,
            } => (normal, abrupt, non_normal),
        }
    }

    /// Sequence `next` after this completion's normal paths. Abrupt paths in
    /// the prefix are retained untouched; only its normal projection reaches
    /// `next`. Callers must still retain the corresponding per-path state
    /// family when the normal and abrupt alternatives have different worlds.
    pub(crate) fn then<M>(self, next: Completion<M, A>) -> Completion<M, A> {
        let (prefix_normal, mut abrupt, mut non_normal) = self.into_alternatives();
        if prefix_normal.is_none() {
            return Completion::<M, A>::from_alternatives(None, abrupt, non_normal);
        }
        let (normal, next_abrupt, next_non_normal) = next.into_alternatives();
        abrupt.extend(next_abrupt);
        non_normal.may_throw_without_payload |= next_non_normal.may_throw_without_payload;
        non_normal.may_diverge |= next_non_normal.may_diverge;
        non_normal.unresolved |= next_non_normal.unresolved;
        Completion::from_alternatives(normal, abrupt, non_normal)
    }

    /// Canonicalize a joined normal/abrupt partition without erasing either
    /// side. The empty partition is an invalid caller state; it is retained as
    /// an empty Alternatives value so release builds never invent completion.
    pub(crate) fn from_alternatives(
        normal: Option<N>,
        mut abrupt: Vec<AbruptAlternative<A>>,
        non_normal: NonNormalPossibilities,
    ) -> Self {
        for alternative in &mut abrupt {
            if let AbruptAlternative::Unknown(Some(values)) = alternative
                && values.0.is_empty()
            {
                // An empty abstract set is bottom, not evidence for a throw.
                // Preserve the known abrupt possibility with the broadest
                // payload rather than allowing it to disappear at a join.
                values.0.insert(AbstractValue::Value(
                    crate::facts::abstract_domain::value::Value::Top,
                ));
            }
        }
        if abrupt.is_empty() && non_normal == NonNormalPossibilities::default() {
            return normal.map_or_else(
                || Self::Alternatives {
                    normal: None,
                    abrupt,
                    non_normal,
                },
                Self::Normal,
            );
        }
        if abrupt.len() == 1 && non_normal == NonNormalPossibilities::default() {
            let only = abrupt.pop().expect("one abrupt alternative was checked");
            match (normal, only) {
                (None, AbruptAlternative::Exact(abrupt)) => Self::Abrupt(abrupt),
                (None, AbruptAlternative::Unknown(Some(values))) => Self::UnknownThrow(values),
                (normal, abrupt) => Self::Alternatives {
                    normal,
                    abrupt: vec![abrupt],
                    non_normal,
                },
            }
        } else {
            Self::Alternatives {
                normal,
                abrupt,
                non_normal,
            }
        }
    }
}

impl Default for Completion<(), ()> {
    fn default() -> Self {
        Self::OpaqueMayNormal(())
    }
}

/// The completion, effects, proof dependencies, and allocation judgment from
/// one transfer. Allocation precision is orthogonal to completion.
#[derive(Clone)]
pub(crate) struct TransferLedger<N, A> {
    pub(crate) completion: Completion<N, A>,
    pub(crate) effects: EffectCoverageFragment,
    pub(crate) dependencies: Box<ProofDependencies>,
    pub(super) allocation: AllocationJudgment,
}

impl<N, A> TransferLedger<N, A> {
    pub(super) fn new(
        completion: Completion<N, A>,
        effects: EffectCoverageFragment,
        dependencies: Box<ProofDependencies>,
        allocation: AllocationJudgment,
    ) -> Self {
        Self {
            completion,
            effects,
            dependencies,
            allocation,
        }
    }

    pub(super) fn opaque(
        normal: N,
        effects: EffectCoverageFragment,
        dependencies: Box<ProofDependencies>,
    ) -> Self {
        Self::new(
            Completion::OpaqueMayNormal(normal),
            effects,
            dependencies,
            AllocationJudgment::Opaque,
        )
    }

    pub(super) fn map_normal<M>(self, map: impl FnOnce(N) -> M) -> TransferLedger<M, A> {
        TransferLedger {
            completion: self.completion.map_normal(map),
            effects: self.effects,
            dependencies: self.dependencies,
            allocation: self.allocation,
        }
    }

    pub(super) fn is_exact_normal(&self) -> bool {
        self.completion.is_exact_normal()
    }

    pub(super) fn is_exact_abrupt(&self) -> bool {
        self.completion.is_exact_abrupt()
    }

    pub(super) fn may_normal(&self) -> Option<&N> {
        self.completion.may_normal()
    }

    pub(super) fn normal_dependencies(&self) -> Option<ProofDependencies> {
        if !self.completion.is_exact_normal() {
            return None;
        }
        let mut dependencies = self.effects.initialization_domain_dependencies()?;
        dependencies.extend(self.dependencies.as_ref());
        Some(dependencies)
    }

    pub(super) fn lexical_selection_dependencies(&self) -> Option<Box<ProofDependencies>> {
        if !self.completion.is_exact_normal() {
            return None;
        }
        let mut dependencies = self.effects.lexical_selection_domain_dependencies()?;
        dependencies.extend(self.dependencies.as_ref());
        Some(dependencies)
    }

    pub(super) fn closed_effect_dependencies(&self) -> Option<ProofDependencies> {
        if !self.completion.is_exact_normal() {
            return None;
        }
        let mut dependencies = self.effects.closed_domain_dependencies()?;
        dependencies.extend(self.dependencies.as_ref());
        Some(dependencies)
    }

    pub(super) fn known_abrupt_dependencies(&self) -> Option<ProofDependencies> {
        if !self.completion.is_exact_abrupt() {
            return None;
        }
        let mut dependencies = self.effects.closed_domain_dependencies()?;
        dependencies.extend(self.dependencies.as_ref());
        Some(dependencies)
    }

    pub(super) fn has_closed_effects(&self) -> bool {
        self.closed_effect_dependencies().is_some()
    }

    pub(super) fn depend_on(&mut self, dependencies: &ProofDependencies) {
        self.dependencies.extend(dependencies);
        self.effects.depend_on(dependencies);
    }

    #[cfg(test)]
    pub(super) fn consume_known_abrupt(&mut self, normal: N) -> bool {
        if self.known_abrupt_dependencies().is_none() {
            return false;
        }
        self.completion = Completion::Normal(normal);
        true
    }

    pub(super) fn seal_provider_local_birth_support(
        &mut self,
        fresh_records: &std::collections::BTreeSet<HeapAddress>,
        borrowed_records: &std::collections::BTreeSet<HeapAddress>,
    ) -> bool {
        self.closed_effect_dependencies().is_some()
            && self
                .effects
                .seal_provider_local_birth_support_domains(fresh_records, borrowed_records)
    }
}

#[cfg(test)]
mod tests {
    use super::super::allocation_transfer::AllocationJudgment;
    use super::super::effect_coverage::EffectCoverageFragment;
    use super::super::value_model::{AbstractValue, Values};
    use super::{
        AbruptAlternative, Completion, NonNormalPossibilities, TransferLedger, UnknownThrowValues,
    };
    use crate::facts::abstract_domain::value::Value;

    #[test]
    fn opaque_prefix_then_throw_has_no_normal_path_and_keeps_unresolved_behavior() {
        let completion: Completion<i32, &'static str> =
            Completion::OpaqueMayNormal(1).then(Completion::Abrupt("throw"));

        let Completion::Alternatives {
            normal,
            abrupt,
            non_normal,
        } = completion
        else {
            panic!("the opaque prefix and exact throw must remain separate")
        };
        assert_eq!(normal, None);
        assert_eq!(abrupt, vec![AbruptAlternative::Exact("throw")]);
        assert_eq!(
            non_normal,
            NonNormalPossibilities {
                may_throw_without_payload: false,
                may_diverge: true,
                unresolved: true,
            }
        );
    }

    #[test]
    fn sequencing_keeps_abrupt_alternatives_and_only_composes_the_normal_path() {
        let prefix = Completion::from_alternatives(
            Some(1),
            vec![AbruptAlternative::Exact("earlier throw")],
            NonNormalPossibilities::default(),
        );
        let next: Completion<i32, &'static str> = Completion::Normal(2);

        let Completion::Alternatives {
            normal,
            abrupt,
            non_normal,
        } = prefix.then(next)
        else {
            panic!("normal and abrupt branches must both remain")
        };
        assert_eq!(normal, Some(2));
        assert_eq!(abrupt, vec![AbruptAlternative::Exact("earlier throw")]);
        assert_eq!(non_normal, NonNormalPossibilities::default());
    }

    #[test]
    fn opaque_normal_projection_resumes_suffix_without_becoming_exact() {
        let completion: Completion<i32, &'static str> =
            Completion::OpaqueMayNormal(1).then(Completion::Normal(2));

        assert!(!completion.requires_path_partition());
        assert!(!completion.is_exact_normal());
        assert_eq!(completion.may_normal(), Some(&2));
        let (normal, abrupt, non_normal) = completion.into_alternatives();
        assert_eq!(normal, Some(2));
        assert!(abrupt.is_empty());
        assert!(non_normal.may_diverge);
        assert!(non_normal.unresolved);
    }

    #[test]
    fn represented_abrupt_alternative_still_requires_world_partition() {
        let completion = Completion::from_alternatives(
            Some(1),
            vec![AbruptAlternative::Exact("throw")],
            NonNormalPossibilities::default(),
        );

        assert!(completion.requires_path_partition());
        assert!(!completion.is_exact_normal());
    }

    #[test]
    fn opaque_normal_projection_cannot_close_effect_receipts() {
        let normal = Values::from([AbstractValue::Value(Value::Top)]);
        let completion = Completion::<Values, &'static str>::OpaqueMayNormal(normal.clone())
            .then(Completion::Normal(normal));
        let ledger = TransferLedger::new(
            completion,
            EffectCoverageFragment::no_effects(),
            Box::default(),
            AllocationJudgment::CoveredAllocationOnly(Box::default()),
        );

        assert!(!ledger.is_exact_normal());
        assert!(ledger.closed_effect_dependencies().is_none());
    }

    #[test]
    fn unknown_throw_payload_does_not_reopen_normal_continuation() {
        let thrown = Values::from([AbstractValue::Value(Value::Top)]);
        let completion: Completion<i32, &'static str> =
            Completion::<i32, &'static str>::UnknownThrow(UnknownThrowValues::new(thrown.clone()))
                .then(Completion::Normal(2));

        assert!(completion.has_no_normal_path());
        assert_eq!(completion.may_normal(), None);
        assert!(matches!(completion, Completion::UnknownThrow(values) if values.0 == thrown));
    }

    #[test]
    fn empty_unknown_throw_payload_is_widened_instead_of_becoming_bottom() {
        let completion: Completion<i32, &'static str> = Completion::from_alternatives(
            None,
            vec![AbruptAlternative::Unknown(Some(UnknownThrowValues::new(
                Values::new(),
            )))],
            NonNormalPossibilities::default(),
        );

        assert!(matches!(
            completion,
            Completion::UnknownThrow(values)
                if values.0 == Values::from([AbstractValue::Value(Value::Top)])
        ));
    }
}
