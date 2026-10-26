//! Lower existing debundle selector specs into the global selector IR.
//!
//! Binding selectors and the relational selector primitives lower into one
//! joint program. `source_match` selectors are matched by the shape matcher;
//! only their candidate rows enter the program.

use std::collections::{BTreeMap, BTreeSet};

use analysis::StatementKind;
use selector_ir::{
    ClaimKind, SelectorAtom, SelectorProgram, SelectorProjectedValue, SelectorTargetId,
    SelectorVariableId, SourceUnitTerm, StringTerm, VariableDomain,
};
use spec::{AnonymousStatementSelector, BindingSelector, BindingSourceKind, CrossRefRelation};

/// A projected `source_match` candidate: its place and, per reference column,
/// the chunk identifier its template bound there.
pub type ProjectedRow = ((analysis::SourceUnitId, String), Vec<String>);

/// A projected `source_matches[]` candidate: one place per target binding and,
/// per reference column, the chunk identifier its template bound there.
pub type ProjectedGroupRow = (Vec<(analysis::SourceUnitId, String)>, Vec<String>);

/// Incremental builder for one joint selector program. Targets are scoped by
/// logical module plus export name, while relation anchors resolve against the
/// target set using the selector family's scoping rules.
#[derive(Debug, Clone, Default)]
pub struct MemberSelectorProgramBuilder {
    program: SelectorProgram,
    source_units_by_export: BTreeMap<(String, String), SelectorVariableId>,
    /// The binding each projected `source_match` export declares, by
    /// (logical module, export name).
    projected_bindings: BTreeMap<(String, String), SelectorVariableId>,
    /// Each projected anonymous statement's source unit variable, by logical module
    /// and statement index.
    projected_anonymous_source_units: BTreeMap<(String, usize), SelectorVariableId>,
    global_source_unit_by_export: BTreeMap<String, Option<SelectorVariableId>>,
    targeted_source_units: BTreeMap<SelectorVariableId, String>,
    injective_targeted_source_units: BTreeSet<SelectorVariableId>,
    source_unit_injectivity_classes: BTreeMap<SelectorVariableId, String>,
}

#[derive(Debug, Clone, Copy)]
pub enum MemberSelectorSpecRef<'a> {
    Binding(&'a BindingSelector),
    SourceMatch(&'a AnonymousStatementSelector),
    CrossRef(&'a spec::CrossRefTarget),
    ReadsMember(&'a spec::ReadsMemberTarget),
    MemberOfModule(&'a spec::MemberOfModuleTarget),
    PassedToCall(&'a spec::PassedToCallTarget),
    MakesDecorateCall(&'a spec::MakesDecorateCallTarget),
    IntrinsicAlias(&'a spec::IntrinsicAliasTarget),
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum SourceUnitInjectivityClass {
    SourceUnit(SelectorVariableId),
    Shared(String),
}

impl MemberSelectorProgramBuilder {
    pub fn declare_member_target(
        &mut self,
        logical_module: impl Into<String>,
        export_name: &str,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        self.declare_target(
            logical_module,
            export_name,
            selector,
            ClaimKind::Binding {
                export_name: Some(export_name.to_string()),
            },
        )
    }

    pub fn declare_binding_group_member_target(
        &mut self,
        logical_module: impl Into<String>,
        export_name: &str,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        self.declare_target(
            logical_module,
            export_name,
            selector,
            ClaimKind::BindingGroupMember {
                export_name: export_name.to_string(),
            },
        )
    }

    fn declare_target(
        &mut self,
        logical_module: impl Into<String>,
        export_name: &str,
        selector: MemberSelectorSpecRef<'_>,
        claim: ClaimKind,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        let logical_module = logical_module.into();
        let source_unit = self.source_unit_for_local_export(&logical_module, export_name);
        self.targeted_source_units
            .insert(source_unit, format!("{logical_module}::{export_name}"));
        if !matches!(selector, MemberSelectorSpecRef::Binding(_)) {
            self.injective_targeted_source_units.insert(source_unit);
        }
        self.global_source_unit_by_export
            .entry(export_name.to_string())
            .and_modify(|slot| {
                if *slot != Some(source_unit) {
                    *slot = None;
                }
            })
            .or_insert(Some(source_unit));
        Ok(self.program.add_target(source_unit, logical_module, claim))
    }

    pub fn declare_projected_anonymous_statement_target_in_module(
        &mut self,
        logical_module: impl Into<String>,
        statement_index: usize,
    ) -> SelectorTargetId {
        let logical_module = logical_module.into();
        let source_unit = self.program.add_variable(
            VariableDomain::SourceUnit,
            Some(format!(
                "{logical_module}::anonymous_statement.projected.{statement_index}"
            )),
        );
        self.projected_anonymous_source_units
            .insert((logical_module.clone(), statement_index), source_unit);
        self.injective_targeted_source_units.insert(source_unit);
        self.program.add_target(
            source_unit,
            logical_module,
            ClaimKind::AnonymousStatement {
                index: statement_index,
            },
        )
    }

    /// The candidate source units of an anonymous statement declared with
    /// [`Self::declare_projected_anonymous_statement_target_in_module`], each
    /// with the value of every `references` variable in that row.
    pub fn lower_projected_anonymous_statement_candidates(
        &mut self,
        logical_module: &str,
        statement_index: usize,
        references: &[SelectorVariableId],
        candidate_rows: Vec<(analysis::SourceUnitId, Vec<String>)>,
    ) {
        let source_unit =
            self.projected_anonymous_source_units[&(logical_module.to_string(), statement_index)];
        self.program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: [source_unit]
                .into_iter()
                .chain(references.iter().copied())
                .collect(),
            rows: candidate_rows
                .into_iter()
                .map(|(source_unit, referenced)| {
                    [SelectorProjectedValue::SourceUnit(source_unit)]
                        .into_iter()
                        .chain(referenced.into_iter().map(SelectorProjectedValue::String))
                        .collect()
                })
                .collect(),
        });
    }

    pub fn lower_member_constraints(
        &mut self,
        logical_module: &str,
        export_name: &str,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<(), SelectorIrLoweringError> {
        let source_unit = self.source_unit_for_local_export(logical_module, export_name);
        self.lower_selector_atoms(logical_module, source_unit, selector)
    }

    /// The binding variable of projected `source_match` export `export_name`,
    /// shared by its own candidate table and every template that references
    /// it.
    pub fn projected_binding_variable(
        &mut self,
        logical_module: &str,
        export_name: &str,
    ) -> SelectorVariableId {
        let key = (logical_module.to_string(), export_name.to_string());
        if let Some(binding) = self.projected_bindings.get(&key) {
            return *binding;
        }
        let binding = self.program.add_variable(
            VariableDomain::String,
            Some(format!(
                "{logical_module}::source_match.projected_binding.{export_name}"
            )),
        );
        self.projected_bindings.insert(key, binding);
        binding
    }

    /// The binding variable of export `export_name`, pinned by a relational
    /// selector, for templates that reference it: the variable ranges over
    /// the bindings its source unit declares.
    pub fn relational_binding_variable(
        &mut self,
        logical_module: &str,
        export_name: &str,
    ) -> SelectorVariableId {
        let key = (logical_module.to_string(), export_name.to_string());
        if let Some(binding) = self.projected_bindings.get(&key) {
            return *binding;
        }
        let binding = self.projected_binding_variable(logical_module, export_name);
        let source_unit = self.source_unit_for_local_export(logical_module, export_name);
        self.program
            .add_atom(SelectorAtom::SourceUnitDeclaresBinding {
                source_unit: source_unit_term(source_unit),
                binding: string_term(binding),
            });
        binding
    }

    /// Each row is a candidate place and, per `references` variable, the chunk
    /// identifier the template bound at that reference.
    pub fn lower_projected_source_match_candidates(
        &mut self,
        logical_module: &str,
        export_name: &str,
        references: &[SelectorVariableId],
        candidate_rows: Vec<ProjectedRow>,
    ) {
        let source_unit = self.source_unit_for_local_export(logical_module, export_name);
        let binding = self.projected_binding_variable(logical_module, export_name);
        self.program
            .add_atom(SelectorAtom::SourceUnitDeclaresBinding {
                source_unit: source_unit_term(source_unit),
                binding: string_term(binding),
            });
        self.program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: [source_unit, binding]
                .into_iter()
                .chain(references.iter().copied())
                .collect(),
            rows: candidate_rows
                .into_iter()
                .map(|((source_unit, binding), referenced)| {
                    [
                        SelectorProjectedValue::SourceUnit(source_unit),
                        SelectorProjectedValue::String(binding),
                    ]
                    .into_iter()
                    .chain(referenced.into_iter().map(SelectorProjectedValue::String))
                    .collect()
                })
                .collect(),
        });
    }

    /// As [`Self::lower_projected_source_match_candidates`], for every
    /// binding of one `source_matches[]` template: each row holds one place
    /// per target binding, in `exports_by_target` order.
    pub fn lower_projected_source_match_group_candidates(
        &mut self,
        logical_module: &str,
        exports_by_target: &BTreeMap<String, String>,
        references: &[SelectorVariableId],
        candidate_rows: Vec<ProjectedGroupRow>,
    ) {
        let mut variables = Vec::new();
        // Selector-local names can recur in separate claims of one module;
        // export names distinguish those claims while grouping one claim's
        // targets into a single ownership class.
        let injectivity_class = format!(
            "{logical_module}|source_matches.projected|{}",
            exports_by_target
                .values()
                .cloned()
                .collect::<Vec<_>>()
                .join(",")
        );
        for export_name in exports_by_target.values() {
            let source_unit = self.source_unit_for_local_export(logical_module, export_name);
            self.source_unit_injectivity_classes
                .insert(source_unit, injectivity_class.clone());
            let binding = self.projected_binding_variable(logical_module, export_name);
            self.program
                .add_atom(SelectorAtom::SourceUnitDeclaresBinding {
                    source_unit: source_unit_term(source_unit),
                    binding: string_term(binding),
                });
            variables.push(source_unit);
            variables.push(binding);
        }
        variables.extend(references.iter().copied());
        self.program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables,
            rows: candidate_rows
                .into_iter()
                .map(|(places, referenced)| {
                    places
                        .into_iter()
                        .flat_map(|(source_unit, binding)| {
                            [
                                SelectorProjectedValue::SourceUnit(source_unit),
                                SelectorProjectedValue::String(binding),
                            ]
                        })
                        .chain(referenced.into_iter().map(SelectorProjectedValue::String))
                        .collect::<Vec<_>>()
                })
                .collect(),
        });
    }

    pub fn into_program(mut self) -> Result<SelectorProgram, SelectorIrLoweringError> {
        for ((logical_module, export_name), source_unit) in &self.source_units_by_export {
            if !self.targeted_source_units.contains_key(source_unit) {
                return Err(SelectorIrLoweringError::DanglingAnchor {
                    logical_module: logical_module.clone(),
                    export_name: export_name.clone(),
                });
            }
        }
        let mut unique_target_by_source_unit_class =
            BTreeMap::<SourceUnitInjectivityClass, SelectorTargetId>::new();
        for target in &self.program.targets {
            if self
                .injective_targeted_source_units
                .contains(&target.source_unit)
            {
                let class = self
                    .source_unit_injectivity_classes
                    .get(&target.source_unit)
                    .cloned()
                    .map(SourceUnitInjectivityClass::Shared)
                    .unwrap_or(SourceUnitInjectivityClass::SourceUnit(target.source_unit));
                unique_target_by_source_unit_class
                    .entry(class)
                    .or_insert(target.id);
            }
        }
        let all_different_targets = unique_target_by_source_unit_class
            .into_values()
            .collect::<Vec<_>>();
        if all_different_targets.len() > 1 {
            self.program.require_all_different(all_different_targets);
        }
        self.program.validate()?;
        Ok(self.program)
    }

    fn source_unit_for_local_export(
        &mut self,
        logical_module: &str,
        export_name: &str,
    ) -> SelectorVariableId {
        let key = (logical_module.to_string(), export_name.to_string());
        if let Some(source_unit) = self.source_units_by_export.get(&key) {
            return *source_unit;
        }
        let source_unit = self.program.add_variable(
            VariableDomain::SourceUnit,
            Some(format!("{logical_module}::@{export_name}")),
        );
        self.source_units_by_export.insert(key, source_unit);
        source_unit
    }

    fn source_unit_for_global_export(
        &mut self,
        export_name: &str,
    ) -> Result<SelectorVariableId, SelectorIrLoweringError> {
        match self
            .global_source_unit_by_export
            .get(export_name)
            .copied()
            .flatten()
        {
            Some(source_unit) => Ok(source_unit),
            None if self.global_source_unit_by_export.contains_key(export_name) => {
                Err(SelectorIrLoweringError::AmbiguousAnchor {
                    export_name: export_name.to_string(),
                })
            }
            None => {
                let source_unit = self
                    .program
                    .add_variable(VariableDomain::SourceUnit, Some(format!("@{export_name}")));
                self.source_units_by_export.insert(
                    ("<global>".to_string(), export_name.to_string()),
                    source_unit,
                );
                Ok(source_unit)
            }
        }
    }

    fn lower_selector_atoms(
        &mut self,
        logical_module: &str,
        source_unit: SelectorVariableId,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<(), SelectorIrLoweringError> {
        match selector {
            MemberSelectorSpecRef::Binding(binding) => {
                self.lower_binding_selector(source_unit, binding)
            }
            MemberSelectorSpecRef::SourceMatch(_) => Err(SelectorIrLoweringError::unsupported(
                "source_match",
                "source_match candidates come from the shape matcher \
                 (lower_projected_source_match_candidates), not selector IR atoms",
            )),
            MemberSelectorSpecRef::CrossRef(target) => {
                let anchor = self.source_unit_for_global_export(&target.anchor)?;
                match target.relation {
                    CrossRefRelation::References => {
                        self.program
                            .add_atom(SelectorAtom::SourceUnitReferencesSourceUnit {
                                source_unit: source_unit_term(source_unit),
                                referenced: source_unit_term(anchor),
                            });
                    }
                    CrossRefRelation::Aliases => {
                        self.program
                            .add_atom(SelectorAtom::SourceUnitAliasesSourceUnit {
                                source_unit: source_unit_term(source_unit),
                                aliased: source_unit_term(anchor),
                            });
                    }
                }
                self.add_kind_atom(source_unit, target.kind);
                Ok(())
            }
            MemberSelectorSpecRef::ReadsMember(target) => {
                if let Some(object) = &target.object {
                    let object = self.source_unit_for_global_export(object)?;
                    self.program
                        .add_atom(SelectorAtom::ReadsMemberOfSourceUnit {
                            source_unit: source_unit_term(source_unit),
                            object: source_unit_term(object),
                            member: const_str(&target.member),
                        });
                } else {
                    self.program.add_atom(SelectorAtom::ReadsMember {
                        source_unit: source_unit_term(source_unit),
                        member: const_str(&target.member),
                    });
                }
                self.add_kind_atom(source_unit, target.kind);
                Ok(())
            }
            MemberSelectorSpecRef::MemberOfModule(target) => {
                self.program.add_atom(SelectorAtom::ConsumesModuleMember {
                    source_unit: source_unit_term(source_unit),
                    module: const_str(&target.module),
                    member: const_str(&target.member),
                });
                self.add_kind_atom(source_unit, target.kind);
                Ok(())
            }
            MemberSelectorSpecRef::PassedToCall(target) => {
                let arg_index = optional_index(target.arg_index)?;
                if let Some(object) = &target.object {
                    let object = self.source_unit_for_global_export(object)?;
                    self.program
                        .add_atom(SelectorAtom::PassedToCallOfSourceUnit {
                            source_unit: source_unit_term(source_unit),
                            callee_object: source_unit_term(object),
                            callee_member: const_str(&target.callee_member),
                            arg_index,
                        });
                } else {
                    self.program.add_atom(SelectorAtom::PassedToCall {
                        source_unit: source_unit_term(source_unit),
                        callee_member: const_str(&target.callee_member),
                        arg_index,
                    });
                }
                self.add_kind_atom(source_unit, target.kind);
                Ok(())
            }
            MemberSelectorSpecRef::MakesDecorateCall(target) => {
                let class_anchor = self.source_unit_for_global_export(&target.class)?;
                self.program
                    .add_atom(SelectorAtom::MakesDecorateCallForSourceUnit {
                        source_unit: source_unit_term(source_unit),
                        class_anchor: source_unit_term(class_anchor),
                        member: target.member.as_deref().map(const_str),
                    });
                self.add_kind_atom(source_unit, target.kind);
                Ok(())
            }
            MemberSelectorSpecRef::IntrinsicAlias(target) => {
                let referenced_by =
                    self.source_unit_for_local_export(logical_module, &target.referenced_by);
                self.program.add_atom(SelectorAtom::IntrinsicAlias {
                    source_unit: source_unit_term(source_unit),
                    property: const_str(&target.property),
                    referenced_by: source_unit_term(referenced_by),
                });
                Ok(())
            }
        }
    }

    fn lower_binding_selector(
        &mut self,
        source_unit: SelectorVariableId,
        selector: &BindingSelector,
    ) -> Result<(), SelectorIrLoweringError> {
        if selector.kind == Some(BindingSourceKind::ImportSpecifier) {
            return Err(SelectorIrLoweringError::unsupported(
                "binding",
                "import_specifier binding selectors need import-source unit fact modeling",
            ));
        }

        self.program
            .add_atom(SelectorAtom::SourceUnitDeclaresBinding {
                source_unit: SourceUnitTerm::Var { id: source_unit },
                binding: const_str(&selector.name),
            });
        self.add_kind_atom(source_unit, selector.kind);
        Ok(())
    }

    fn add_kind_atom(&mut self, source_unit: SelectorVariableId, kind: Option<BindingSourceKind>) {
        if let Some(kind) = kind {
            self.program.add_atom(SelectorAtom::SourceUnitKind {
                source_unit: source_unit_term(source_unit),
                statement_kind: const_str(statement_kind_str_for_spec(kind)),
            });
        }
    }
}

fn source_unit_term(source_unit: SelectorVariableId) -> SourceUnitTerm {
    SourceUnitTerm::Var { id: source_unit }
}

fn string_term(string: SelectorVariableId) -> StringTerm {
    StringTerm::Var { id: string }
}

fn const_str(value: &str) -> StringTerm {
    StringTerm::Const {
        value: value.to_string(),
    }
}

fn optional_index(index: Option<usize>) -> Result<Option<u32>, SelectorIrLoweringError> {
    index
        .map(|index| {
            u32::try_from(index).map_err(|_| SelectorIrLoweringError::Unsupported {
                selector_kind: "passed_to_call",
                reason: "arg_index exceeds solver u32 range",
            })
        })
        .transpose()
}

fn statement_kind_str_for_spec(kind: BindingSourceKind) -> &'static str {
    let statement_kind = match kind {
        BindingSourceKind::VariableDeclarator => StatementKind::VarDecl,
        BindingSourceKind::FunctionDeclaration => StatementKind::FnDecl,
        BindingSourceKind::ClassDeclaration => StatementKind::ClassDecl,
        BindingSourceKind::ImportSpecifier => StatementKind::Import,
    };
    statement_kind.into()
}

#[derive(thiserror::Error, Debug, Clone, PartialEq, Eq)]
pub enum SelectorIrLoweringError {
    #[error("unsupported selector IR lowering for {selector_kind}: {reason}")]
    Unsupported {
        selector_kind: &'static str,
        reason: &'static str,
    },
    #[error(
        "cross_ref/reads_member/passed_to_call/makes_decorate_call/intrinsic_alias \
                 selector anchor `@{export_name}` does not name a lowered member in module \
                 {logical_module}"
    )]
    DanglingAnchor {
        logical_module: String,
        export_name: String,
    },
    #[error(
        "cross_ref/reads_member/passed_to_call/makes_decorate_call/intrinsic_alias \
                 selector anchor `@{export_name}` is ambiguous across lowered members"
    )]
    AmbiguousAnchor { export_name: String },
    #[error("invalid selector IR program: {0}")]
    InvalidProgram(selector_ir::SelectorProgramError),
}

impl SelectorIrLoweringError {
    fn unsupported(selector_kind: &'static str, reason: &'static str) -> Self {
        Self::Unsupported {
            selector_kind,
            reason,
        }
    }
}

impl From<selector_ir::SelectorProgramError> for SelectorIrLoweringError {
    fn from(error: selector_ir::SelectorProgramError) -> Self {
        Self::InvalidProgram(error)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use spec::CrossRefTarget;

    const MODULE: &str = "runtime/widgets";

    fn lower(
        builder: &mut MemberSelectorProgramBuilder,
        export_name: &str,
        selector: MemberSelectorSpecRef<'_>,
    ) -> Result<SelectorTargetId, SelectorIrLoweringError> {
        let target = builder.declare_member_target(MODULE, export_name, selector)?;
        builder.lower_member_constraints(MODULE, export_name, selector)?;
        Ok(target)
    }

    fn binding(name: &str) -> BindingSelector {
        BindingSelector {
            name: name.to_string(),
            kind: None,
        }
    }

    fn references(anchor: &str) -> CrossRefTarget {
        CrossRefTarget {
            relation: CrossRefRelation::References,
            anchor: anchor.to_string(),
            kind: Some(BindingSourceKind::FunctionDeclaration),
        }
    }

    #[test]
    fn import_specifier_binding_fails_closed_for_now() {
        let selector = BindingSelector {
            name: "a".to_string(),
            kind: Some(BindingSourceKind::ImportSpecifier),
        };
        let error = lower(
            &mut MemberSelectorProgramBuilder::default(),
            "ImportedWidget",
            MemberSelectorSpecRef::Binding(&selector),
        )
        .unwrap_err();

        assert_eq!(
            error,
            SelectorIrLoweringError::Unsupported {
                selector_kind: "binding",
                reason: "import_specifier binding selectors need import-source unit fact modeling",
            }
        );
    }

    #[test]
    fn joint_builder_reuses_anchor_source_unit_variable() {
        let mut builder = MemberSelectorProgramBuilder::default();
        let anchor = lower(
            &mut builder,
            "Anchor",
            MemberSelectorSpecRef::Binding(&binding("a")),
        )
        .unwrap();
        let delegator = lower(
            &mut builder,
            "Delegator",
            MemberSelectorSpecRef::CrossRef(&references("Anchor")),
        )
        .unwrap();

        let program = builder.into_program().unwrap();
        let anchor_source_unit = program.targets[anchor.0].source_unit;
        let delegator_source_unit = program.targets[delegator.0].source_unit;

        assert_eq!(program.variables.len(), 2);
        assert!(
            program.all_different.is_empty(),
            "plain binding targets are not source_unit-injective by themselves"
        );
        assert!(matches!(
            program.atoms.iter().find(|atom| matches!(atom, SelectorAtom::SourceUnitReferencesSourceUnit { .. })),
            Some(SelectorAtom::SourceUnitReferencesSourceUnit {
                source_unit: SourceUnitTerm::Var { id: source_unit_id },
                referenced: SourceUnitTerm::Var { id: referenced_id },
            }) if *source_unit_id == delegator_source_unit && *referenced_id == anchor_source_unit
        ));
    }

    #[test]
    fn relational_targets_remain_source_unit_injective_without_binding_anchors() {
        let mut builder = MemberSelectorProgramBuilder::default();
        let anchor = lower(
            &mut builder,
            "Anchor",
            MemberSelectorSpecRef::Binding(&binding("a")),
        )
        .unwrap();
        let first = lower(
            &mut builder,
            "First",
            MemberSelectorSpecRef::CrossRef(&references("Anchor")),
        )
        .unwrap();
        let second = lower(
            &mut builder,
            "Second",
            MemberSelectorSpecRef::CrossRef(&references("Anchor")),
        )
        .unwrap();

        let program = builder.into_program().unwrap();

        assert_eq!(program.all_different, vec![vec![first, second]]);
        assert!(
            !program
                .all_different
                .iter()
                .flatten()
                .any(|target| *target == anchor),
            "plain binding anchors should not participate in target source unit injectivity"
        );
    }
}
