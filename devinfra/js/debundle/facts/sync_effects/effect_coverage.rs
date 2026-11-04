//! Nonterminal receipts from operations executed by the ordered local kernel.

use crate::analysis_inputs::ProofDependencies;

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

/// This describes only the operations of one execution. It never identifies
/// or certifies a whole source owner, and may travel through invocation memos.
#[derive(Debug, Clone, Default)]
pub(super) struct EffectCoverageFragment {
    global_properties: DomainCoverage,
    general_heap: DomainCoverage,
    host_effects: DomainCoverage,
    completion: Completion,
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
            completion: Completion::Normal,
        }
    }

    pub(super) fn compose(&mut self, next: &Self) {
        self.global_properties.compose(&next.global_properties);
        self.general_heap.compose(&next.general_heap);
        self.host_effects.compose(&next.host_effects);
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ProgramPremiseKey;

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
