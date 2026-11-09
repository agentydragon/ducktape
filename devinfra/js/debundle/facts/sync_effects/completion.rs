//! Normal, abrupt, or conservatively mixed completion for one synchronous transfer.

use super::allocation_transfer::AllocationJudgment;
use super::effect_coverage::EffectCoverageFragment;
use super::value_model::{AbstractValue, Values};
use crate::facts::abstract_domain::value::Value;
use crate::facts::statement_facts::HeapContentAccesses;

/// A completion summary. `OpaqueMayNormal` preserves a possible normal
/// payload while recording unresolved behavior separately from throws.
#[must_use]
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Completion<N, A> {
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
pub(super) enum AbruptAlternative<A> {
    Exact(A),
    /// `None` means an abrupt possibility is known but its payload was not
    /// represented. `Some` retains actual may-thrown values without inventing
    /// a source origin or frontier.
    Unknown(Option<UnknownThrowValues>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct UnknownThrowValues(Values);

/// Possible behaviors not represented by an exact throw alternative. A
/// generic opaque result does not prove a throw; another path may diverge or
/// have a non-normal behavior the transfer cannot classify.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(super) struct NonNormalPossibilities {
    /// Typed evidence says a throw is possible, but no payload or exact
    /// source witness is represented.
    pub(super) may_throw_without_payload: bool,
    /// A path may fail to produce any synchronous completion.
    pub(super) may_diverge: bool,
    /// Some non-normal behavior exists but is not classified as a throw.
    /// Catch-sensitive consumers must conservatively refuse this path.
    pub(super) unresolved: bool,
}

impl<N, A> Completion<N, A> {
    pub(super) fn is_exact_normal(&self) -> bool {
        matches!(self, Self::Normal(_))
    }

    pub(super) fn has_no_normal_path(&self) -> bool {
        matches!(self, Self::UnknownThrow(_) | Self::Abrupt(_))
            || matches!(self, Self::Alternatives { normal: None, .. })
    }

    /// Whether a non-normal path is represented beside the normal one.
    pub(super) fn may_be_abrupt(&self) -> bool {
        match self {
            Self::Normal(_) => false,
            Self::UnknownThrow(_) | Self::Abrupt(_) | Self::OpaqueMayNormal(_) => true,
            Self::Alternatives {
                abrupt, non_normal, ..
            } => !abrupt.is_empty() || *non_normal != NonNormalPossibilities::default(),
        }
    }

    /// The exact abrupt completions among the represented paths.
    pub(super) fn exact_abrupt(&self) -> Vec<&A> {
        match self {
            Self::Normal(_) | Self::UnknownThrow(_) | Self::OpaqueMayNormal(_) => Vec::new(),
            Self::Abrupt(abrupt) => vec![abrupt],
            Self::Alternatives { abrupt, .. } => abrupt
                .iter()
                .filter_map(|alternative| match alternative {
                    AbruptAlternative::Exact(abrupt) => Some(abrupt),
                    AbruptAlternative::Unknown(_) => None,
                })
                .collect(),
        }
    }

    pub(super) fn may_normal(&self) -> Option<&N> {
        match self {
            Self::Normal(value) | Self::OpaqueMayNormal(value) => Some(value),
            Self::UnknownThrow(_) | Self::Abrupt(_) => None,
            Self::Alternatives { normal, .. } => normal.as_ref(),
        }
    }

    /// Expand compact completion states into normal, exact abrupt, and
    /// unresolved non-normal projections. Opaque normality may coexist with
    /// other behavior, but does not prove that a throw is among those paths.
    pub(super) fn into_alternatives(
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
    pub(super) fn then<M>(self, next: Completion<M, A>) -> Completion<M, A> {
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
    pub(super) fn from_alternatives(
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
                values.0.insert(AbstractValue::Value(Value::Top));
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

/// Absent evidence is the conservative mixed state, never exact normality.
impl Default for Completion<(), ()> {
    fn default() -> Self {
        Self::OpaqueMayNormal(())
    }
}

/// The completion, effects and allocation judgment of one transfer.
/// Allocation precision is orthogonal to completion: an abrupt path makes the
/// judgment opaque, but a covered allocation never proves normal completion.
#[must_use]
#[derive(Clone)]
pub(super) struct TransferLedger<N, A> {
    pub(super) completion: Completion<N, A>,
    pub(super) effects: EffectCoverageFragment,
    pub(super) allocation: AllocationJudgment,
    /// Heap cells this transfer read or wrote. Whether the path was assumed,
    /// and whether an isolated scope keeps them private, is the adopter's
    /// decision.
    pub(super) heap_accesses: HeapContentAccesses,
}

impl<N, A> TransferLedger<N, A> {
    pub(super) fn new(
        completion: Completion<N, A>,
        effects: EffectCoverageFragment,
        allocation: AllocationJudgment,
    ) -> Self {
        Self {
            completion,
            effects,
            allocation,
            heap_accesses: HeapContentAccesses::default(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::value_model::{AbstractValue, Values};
    use super::{AbruptAlternative, Completion, NonNormalPossibilities, UnknownThrowValues};
    use crate::facts::abstract_domain::value::Value;

    #[test]
    fn opaque_prefix_then_throw_has_no_normal_path_and_keeps_unresolved_behavior() {
        let completion: Completion<i32, &'static str> =
            Completion::OpaqueMayNormal(1).then(Completion::Abrupt("throw"));

        assert!(completion.has_no_normal_path());
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
    fn abrupt_prefix_ignores_an_opaque_suffix() {
        let completion = Completion::<(), ()>::Abrupt(()).then(Completion::default());

        assert_eq!(completion, Completion::Abrupt(()));
    }

    #[test]
    fn opaque_normal_projection_resumes_suffix_without_becoming_exact() {
        let completion: Completion<i32, &'static str> =
            Completion::OpaqueMayNormal(1).then(Completion::Normal(2));

        assert!(!completion.is_exact_normal());
        assert!(!completion.has_no_normal_path());
        let (normal, abrupt, non_normal) = completion.into_alternatives();
        assert_eq!(normal, Some(2));
        assert!(abrupt.is_empty());
        assert!(non_normal.may_diverge);
        assert!(non_normal.unresolved);
    }

    #[test]
    fn unknown_throw_payload_does_not_reopen_normal_continuation() {
        let thrown = Values::from([AbstractValue::Value(Value::Top)]);
        let completion: Completion<i32, &'static str> =
            Completion::<i32, &'static str>::UnknownThrow(UnknownThrowValues(thrown.clone()))
                .then(Completion::Normal(2));

        assert!(completion.has_no_normal_path());
        assert!(matches!(completion, Completion::UnknownThrow(values) if values.0 == thrown));
    }

    #[test]
    fn empty_unknown_throw_payload_is_widened_instead_of_becoming_bottom() {
        let completion: Completion<i32, &'static str> = Completion::from_alternatives(
            None,
            vec![AbruptAlternative::Unknown(Some(UnknownThrowValues(
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
