//! Fact indexing: raw fact tables, derived relations and per-relation support indexes.

use std::collections::{BTreeMap, BTreeSet};

use selector_ir::{
    SelectorAtom, SelectorFact, SelectorFactStore, SelectorOperand, SelectorProgram,
    SelectorProjectedValue, VariableDomain,
};
use source_identity::{SourceUnitId, SourceUnitOrdinal};

#[derive(Debug, Default)]
pub(super) struct SourceUnitStringIndex {
    pub(super) source_units_by_value: BTreeMap<String, Vec<SourceUnitId>>,
    pub(super) values_by_unit: BTreeMap<SourceUnitId, Vec<String>>,
}

impl SourceUnitStringIndex {
    fn from_facts(facts: BTreeSet<(SourceUnitId, String)>) -> Self {
        let mut index = Self::default();
        // Tuple order makes both per-key lists sorted and unique.
        for (unit, value) in facts {
            index
                .source_units_by_value
                .entry(value.clone())
                .or_default()
                .push(unit);
            index.values_by_unit.entry(unit).or_default().push(value);
        }
        index
    }

    pub(super) fn rows(&self) -> impl Iterator<Item = (SourceUnitId, &str)> {
        self.values_by_unit
            .iter()
            .flat_map(|(&unit, values)| values.iter().map(move |value| (unit, value.as_str())))
    }

    pub(super) fn contains(&self, source_unit: SourceUnitId, value: &str) -> bool {
        self.values_by_unit.get(&source_unit).is_some_and(|values| {
            values
                .binary_search_by(|candidate| candidate.as_str().cmp(value))
                .is_ok()
        })
    }
}

#[derive(Debug, Default)]
pub(super) struct RelationSupportCache {
    empty_source_unit_support: BTreeSet<SourceUnitId>,
    empty_source_unit_pair_support: BTreeSet<(SourceUnitId, SourceUnitId)>,
    module_member_uses_by_module_member: BTreeMap<(String, String), BTreeSet<SourceUnitId>>,
    reads_member_of_source_unit_by_member: BTreeMap<String, BTreeSet<(SourceUnitId, SourceUnitId)>>,
    call_arguments_by_member: BTreeMap<String, BTreeSet<SourceUnitId>>,
    call_arguments_by_member_arg_index: BTreeMap<String, BTreeMap<usize, BTreeSet<SourceUnitId>>>,
    call_arguments_from_source_unit_by_member:
        BTreeMap<String, BTreeSet<(SourceUnitId, SourceUnitId)>>,
    call_arguments_from_source_unit_by_member_arg_index:
        BTreeMap<String, BTreeMap<usize, BTreeSet<(SourceUnitId, SourceUnitId)>>>,
    makes_decorate_call_for_source_unit_all: BTreeSet<(SourceUnitId, SourceUnitId)>,
    makes_decorate_call_for_source_unit_by_member:
        BTreeMap<String, BTreeSet<(SourceUnitId, SourceUnitId)>>,
    intrinsic_alias_referenced_by_property:
        BTreeMap<String, BTreeSet<(SourceUnitId, SourceUnitId)>>,
}

impl RelationSupportCache {
    fn from_domains(domains: &FactDomains) -> Self {
        let mut cache = Self::default();

        for (source_unit, module, member) in &domains.module_member_uses {
            cache
                .module_member_uses_by_module_member
                .entry((module.clone(), member.clone()))
                .or_default()
                .insert(*source_unit);
        }

        for (source_unit, object, member) in &domains.reads_member_of_unit {
            cache
                .reads_member_of_source_unit_by_member
                .entry(member.clone())
                .or_default()
                .insert((*source_unit, *object));
        }

        for (source_unit, callee_member, arg_index) in &domains.call_arguments {
            cache
                .call_arguments_by_member
                .entry(callee_member.clone())
                .or_default()
                .insert(*source_unit);
            cache
                .call_arguments_by_member_arg_index
                .entry(callee_member.clone())
                .or_default()
                .entry(*arg_index)
                .or_default()
                .insert(*source_unit);
        }

        for (source_unit, callee_object, callee_member, arg_index) in
            &domains.call_arguments_from_unit
        {
            let support = (*source_unit, *callee_object);
            cache
                .call_arguments_from_source_unit_by_member
                .entry(callee_member.clone())
                .or_default()
                .insert(support);
            cache
                .call_arguments_from_source_unit_by_member_arg_index
                .entry(callee_member.clone())
                .or_default()
                .entry(*arg_index)
                .or_default()
                .insert(support);
        }

        for (source_unit, class_anchor, member) in &domains.makes_decorate_call_for_unit {
            let support = (*source_unit, *class_anchor);
            cache
                .makes_decorate_call_for_source_unit_all
                .insert(support);
            if let Some(member) = member {
                cache
                    .makes_decorate_call_for_source_unit_by_member
                    .entry(member.clone())
                    .or_default()
                    .insert(support);
            }
        }

        for (source_unit, property, referenced_by) in &domains.intrinsic_alias_referenced_by {
            cache
                .intrinsic_alias_referenced_by_property
                .entry(property.clone())
                .or_default()
                .insert((*source_unit, *referenced_by));
        }

        cache
    }

    pub(super) fn module_member_uses(&self, module: &str, member: &str) -> &BTreeSet<SourceUnitId> {
        self.module_member_uses_by_module_member
            .get(&(module.to_string(), member.to_string()))
            .unwrap_or(&self.empty_source_unit_support)
    }

    pub(super) fn reads_member_of_source_unit(
        &self,
        member: &str,
    ) -> &BTreeSet<(SourceUnitId, SourceUnitId)> {
        self.reads_member_of_source_unit_by_member
            .get(member)
            .unwrap_or(&self.empty_source_unit_pair_support)
    }

    pub(super) fn call_arguments(
        &self,
        callee_member: &str,
        arg_index: Option<u32>,
    ) -> &BTreeSet<SourceUnitId> {
        match arg_index {
            Some(arg_index) => self
                .call_arguments_by_member_arg_index
                .get(callee_member)
                .and_then(|by_arg_index| {
                    usize::try_from(arg_index)
                        .ok()
                        .and_then(|arg_index| by_arg_index.get(&arg_index))
                })
                .unwrap_or(&self.empty_source_unit_support),
            None => self
                .call_arguments_by_member
                .get(callee_member)
                .unwrap_or(&self.empty_source_unit_support),
        }
    }

    pub(super) fn call_arguments_from_source_unit(
        &self,
        callee_member: &str,
        arg_index: Option<u32>,
    ) -> &BTreeSet<(SourceUnitId, SourceUnitId)> {
        match arg_index {
            Some(arg_index) => self
                .call_arguments_from_source_unit_by_member_arg_index
                .get(callee_member)
                .and_then(|by_arg_index| {
                    usize::try_from(arg_index)
                        .ok()
                        .and_then(|arg_index| by_arg_index.get(&arg_index))
                })
                .unwrap_or(&self.empty_source_unit_pair_support),
            None => self
                .call_arguments_from_source_unit_by_member
                .get(callee_member)
                .unwrap_or(&self.empty_source_unit_pair_support),
        }
    }

    pub(super) fn makes_decorate_call_for_source_unit(
        &self,
        member: Option<&str>,
    ) -> &BTreeSet<(SourceUnitId, SourceUnitId)> {
        match member {
            Some(member) => self
                .makes_decorate_call_for_source_unit_by_member
                .get(member)
                .unwrap_or(&self.empty_source_unit_pair_support),
            None => &self.makes_decorate_call_for_source_unit_all,
        }
    }

    pub(super) fn intrinsic_alias_referenced_by(
        &self,
        property: &str,
    ) -> &BTreeSet<(SourceUnitId, SourceUnitId)> {
        self.intrinsic_alias_referenced_by_property
            .get(property)
            .unwrap_or(&self.empty_source_unit_pair_support)
    }
}

#[derive(Debug, Default)]
struct DerivedFactRequirements {
    references_unit: bool,
    member_reads: bool,
    reads_member_of_unit: bool,
    module_member_uses: bool,
    call_arguments: bool,
    call_arguments_from_unit: bool,
    makes_decorate_call_for_unit: bool,
    intrinsic_alias_referenced_by: bool,
}

impl DerivedFactRequirements {
    fn from_program(program: &SelectorProgram) -> Self {
        let mut requirements = Self::default();
        for atom in &program.atoms {
            match atom {
                SelectorAtom::SourceUnitReferencesSourceUnit { .. } => {
                    requirements.references_unit = true;
                }
                SelectorAtom::ReadsMember { .. } => {
                    requirements.member_reads = true;
                }
                SelectorAtom::ReadsMemberOfSourceUnit { .. } => {
                    requirements.reads_member_of_unit = true;
                }
                SelectorAtom::ConsumesModuleMember { .. } => {
                    requirements.module_member_uses = true;
                }
                SelectorAtom::PassedToCall { .. } => {
                    requirements.call_arguments = true;
                }
                SelectorAtom::PassedToCallOfSourceUnit { .. } => {
                    requirements.call_arguments_from_unit = true;
                }
                SelectorAtom::MakesDecorateCallForSourceUnit { .. } => {
                    requirements.makes_decorate_call_for_unit = true;
                }
                SelectorAtom::IntrinsicAlias { .. } => {
                    requirements.intrinsic_alias_referenced_by = true;
                }
                SelectorAtom::SourceUnitKind { .. }
                | SelectorAtom::SourceUnitDeclaresBinding { .. }
                | SelectorAtom::ProjectedAllowedTuples { .. }
                | SelectorAtom::SourceUnitNotIn { .. } => {}
            }
        }
        requirements
    }
}

#[derive(Debug, Default)]
pub(super) struct FactDomains {
    source_units: BTreeSet<SourceUnitId>,
    strings: BTreeSet<String>,
    source_unit_statement_ordinals: BTreeSet<(SourceUnitId, SourceUnitOrdinal)>,
    raw_source_unit_references_binding: BTreeSet<(SourceUnitId, String)>,
    pub(super) references_unit: BTreeSet<(SourceUnitId, SourceUnitId)>,
    raw_member_reads: BTreeSet<(SourceUnitOrdinal, Option<String>, String)>,
    pub(super) member_reads: BTreeSet<(SourceUnitId, String)>,
    reads_member_of_unit: BTreeSet<(SourceUnitId, SourceUnitId, String)>,
    raw_module_member_uses: BTreeSet<(SourceUnitOrdinal, String, String)>,
    module_member_uses: BTreeSet<(SourceUnitId, String, String)>,
    raw_call_arguments: BTreeSet<(String, Option<String>, String, usize)>,
    call_arguments: BTreeSet<(SourceUnitId, String, usize)>,
    call_arguments_from_unit: BTreeSet<(SourceUnitId, SourceUnitId, String, usize)>,
    decorate_calls: BTreeSet<(String, String, Option<String>)>,
    makes_decorate_call_for_unit: BTreeSet<(SourceUnitId, SourceUnitId, Option<String>)>,
    intrinsic_aliases: BTreeSet<(String, String)>,
    intrinsic_alias_referenced_by: BTreeSet<(SourceUnitId, String, SourceUnitId)>,
    pub(super) source_unit_kinds_index: SourceUnitStringIndex,
    pub(super) declared_bindings_index: SourceUnitStringIndex,
    pub(super) relation_supports: RelationSupportCache,
}

impl FactDomains {
    pub(super) fn from_program_and_facts(
        program: &SelectorProgram,
        facts: &SelectorFactStore,
    ) -> Self {
        let mut domains = Self::default();
        let requirements = DerivedFactRequirements::from_program(program);
        domains.add_facts(facts);
        domains.add_derived_facts(&requirements);
        domains.add_program_constants(program);
        domains.relation_supports = RelationSupportCache::from_domains(&domains);
        domains
    }

    pub(super) fn values_for(&self, domain: VariableDomain) -> Vec<SelectorProjectedValue> {
        match domain {
            VariableDomain::SourceUnit => self
                .source_units
                .iter()
                .copied()
                .map(SelectorProjectedValue::SourceUnit)
                .collect(),
            VariableDomain::String => self
                .strings
                .iter()
                .cloned()
                .map(SelectorProjectedValue::String)
                .collect(),
        }
    }

    fn add_facts(&mut self, facts: &SelectorFactStore) {
        let mut source_unit_kinds = BTreeSet::new();
        let mut declared_bindings = BTreeSet::new();
        for fact in &facts.facts {
            match fact {
                SelectorFact::SourceUnit {
                    source_unit,
                    statement_ordinal,
                    statement_kind,
                    ..
                } => {
                    self.add_source_unit(*source_unit);
                    self.add_string(statement_kind);
                    source_unit_kinds.insert((*source_unit, statement_kind.clone()));
                    self.source_unit_statement_ordinals
                        .insert((*source_unit, *statement_ordinal));
                }
                SelectorFact::DeclaredBinding {
                    source_unit,
                    binding,
                    ..
                } => {
                    self.add_source_unit(*source_unit);
                    self.add_string(binding);
                    declared_bindings.insert((*source_unit, binding.clone()));
                }
                SelectorFact::SourceUnitReferencesBinding {
                    source_unit,
                    binding,
                } => {
                    self.add_source_unit(*source_unit);
                    self.add_string(binding);
                    self.raw_source_unit_references_binding
                        .insert((*source_unit, binding.clone()));
                }
                SelectorFact::MemberRead {
                    statement_ordinal,
                    object,
                    member,
                    ..
                } => {
                    if let Some(object) = object {
                        self.add_string(object);
                    }
                    self.add_string(member);
                    self.raw_member_reads.insert((
                        *statement_ordinal,
                        object.clone(),
                        member.clone(),
                    ));
                }
                SelectorFact::ModuleMemberUse {
                    statement_ordinal,
                    module,
                    member,
                    ..
                } => {
                    self.add_string(module);
                    self.add_string(member);
                    self.raw_module_member_uses.insert((
                        *statement_ordinal,
                        module.clone(),
                        member.clone(),
                    ));
                }
                SelectorFact::CallArgumentUse {
                    argument,
                    callee_object,
                    callee_member,
                    arg_index,
                    ..
                } => {
                    self.add_string(argument);
                    if let Some(callee_object) = callee_object {
                        self.add_string(callee_object);
                    }
                    self.add_string(callee_member);
                    self.raw_call_arguments.insert((
                        argument.clone(),
                        callee_object.clone(),
                        callee_member.clone(),
                        *arg_index,
                    ));
                }
                SelectorFact::DecorateCallUse {
                    callee,
                    class_anchor,
                    member,
                    ..
                } => {
                    self.add_string(callee);
                    self.add_string(class_anchor);
                    if let Some(member) = member {
                        self.add_string(member);
                    }
                    self.decorate_calls.insert((
                        callee.clone(),
                        class_anchor.clone(),
                        member.clone(),
                    ));
                }
                SelectorFact::IntrinsicAliasUse {
                    binding, property, ..
                } => {
                    self.add_string(binding);
                    self.add_string(property);
                    self.intrinsic_aliases
                        .insert((binding.clone(), property.clone()));
                }
            }
        }
        self.source_unit_kinds_index = SourceUnitStringIndex::from_facts(source_unit_kinds);
        self.declared_bindings_index = SourceUnitStringIndex::from_facts(declared_bindings);
    }

    pub(super) fn discard_unneeded_raw_relations(&mut self) {
        self.raw_source_unit_references_binding.clear();
        self.raw_member_reads.clear();
        self.raw_module_member_uses.clear();
        self.raw_call_arguments.clear();
        self.decorate_calls.clear();
        self.intrinsic_aliases.clear();
    }

    pub(super) fn discard_full_domain_source_sets(&mut self) {
        self.source_units.clear();
        self.strings.clear();
    }

    fn add_derived_facts(&mut self, requirements: &DerivedFactRequirements) {
        let source_units_by_binding = &self.declared_bindings_index.source_units_by_value;
        if requirements.references_unit {
            for (source_unit, binding) in &self.raw_source_unit_references_binding {
                if !self
                    .declared_bindings_index
                    .values_by_unit
                    .contains_key(source_unit)
                {
                    continue;
                }
                if let Some(referenced_source_units) = source_units_by_binding.get(binding) {
                    self.references_unit.extend(
                        referenced_source_units
                            .iter()
                            .map(|referenced| (*source_unit, *referenced)),
                    );
                }
            }
        }

        let needs_source_unit_by_ordinal = requirements.member_reads
            || requirements.reads_member_of_unit
            || requirements.module_member_uses;
        let source_unit_by_ordinal = needs_source_unit_by_ordinal.then(|| {
            self.source_unit_statement_ordinals
                .iter()
                .map(|(source_unit, ordinal)| (*ordinal, *source_unit))
                .collect::<BTreeMap<_, _>>()
        });

        // Reads of `object.member` by a declaring source unit, which
        // `reads_member_of_unit` joins to the source units declaring `object`.
        let mut member_reads_from_binding = BTreeSet::new();
        if requirements.member_reads || requirements.reads_member_of_unit {
            let source_unit_by_ordinal = source_unit_by_ordinal
                .as_ref()
                .expect("source unit ordinal index should be built for member reads");
            for (statement_ordinal, object, member) in &self.raw_member_reads {
                let Some(source_unit) = source_unit_by_ordinal.get(statement_ordinal) else {
                    continue;
                };
                if !self
                    .declared_bindings_index
                    .values_by_unit
                    .contains_key(source_unit)
                {
                    continue;
                }
                if requirements.member_reads {
                    self.member_reads.insert((*source_unit, member.clone()));
                }
                if requirements.reads_member_of_unit
                    && let Some(object) = object
                {
                    member_reads_from_binding.insert((
                        *source_unit,
                        object.clone(),
                        member.clone(),
                    ));
                }
            }
        }

        if requirements.reads_member_of_unit {
            for (source_unit, object_binding, member) in &member_reads_from_binding {
                if let Some(object_source_units) = source_units_by_binding.get(object_binding) {
                    self.reads_member_of_unit
                        .extend(object_source_units.iter().map(|object_source_unit| {
                            (*source_unit, *object_source_unit, member.clone())
                        }));
                }
            }
        }

        if requirements.module_member_uses {
            let source_unit_by_ordinal = source_unit_by_ordinal
                .as_ref()
                .expect("source unit ordinal index should be built for module member uses");
            for (statement_ordinal, module, member) in &self.raw_module_member_uses {
                let Some(source_unit) = source_unit_by_ordinal.get(statement_ordinal) else {
                    continue;
                };
                if !self
                    .declared_bindings_index
                    .values_by_unit
                    .contains_key(source_unit)
                {
                    continue;
                }
                self.module_member_uses
                    .insert((*source_unit, module.clone(), member.clone()));
            }
        }

        if requirements.call_arguments || requirements.call_arguments_from_unit {
            for (argument, callee_object, callee_member, arg_index) in &self.raw_call_arguments {
                let Some(argument_source_units) = source_units_by_binding.get(argument) else {
                    continue;
                };
                for source_unit in argument_source_units {
                    if requirements.call_arguments {
                        self.call_arguments.insert((
                            *source_unit,
                            callee_member.clone(),
                            *arg_index,
                        ));
                    }
                    if requirements.call_arguments_from_unit
                        && let Some(callee_object) = callee_object
                        && let Some(callee_object_source_units) =
                            source_units_by_binding.get(callee_object)
                    {
                        self.call_arguments_from_unit.extend(
                            callee_object_source_units
                                .iter()
                                .map(|callee_object_source_unit| {
                                    (
                                        *source_unit,
                                        *callee_object_source_unit,
                                        callee_member.clone(),
                                        *arg_index,
                                    )
                                }),
                        );
                    }
                }
            }
        }

        if requirements.makes_decorate_call_for_unit {
            for (callee, class_anchor, member) in &self.decorate_calls {
                let Some(callee_source_units) = source_units_by_binding.get(callee) else {
                    continue;
                };
                for source_unit in callee_source_units {
                    if let Some(class_source_units) = source_units_by_binding.get(class_anchor) {
                        self.makes_decorate_call_for_unit
                            .extend(class_source_units.iter().map(|class_source_unit| {
                                (*source_unit, *class_source_unit, member.clone())
                            }));
                    }
                }
            }
        }

        if requirements.intrinsic_alias_referenced_by {
            let mut raw_referencers_by_binding: BTreeMap<&str, Vec<SourceUnitId>> = BTreeMap::new();
            for (referencer, binding) in &self.raw_source_unit_references_binding {
                raw_referencers_by_binding
                    .entry(binding.as_str())
                    .or_default()
                    .push(*referencer);
            }
            for (binding, property) in &self.intrinsic_aliases {
                let Some(alias_source_units) = source_units_by_binding.get(binding) else {
                    continue;
                };
                if let Some(referencers) = raw_referencers_by_binding.get(binding.as_str()) {
                    self.intrinsic_alias_referenced_by
                        .extend(alias_source_units.iter().flat_map(|alias_source_unit| {
                            referencers.iter().map(|referencer| {
                                (*alias_source_unit, property.clone(), *referencer)
                            })
                        }));
                }
            }
        }
    }

    fn add_program_constants(&mut self, program: &SelectorProgram) {
        for operand in program.atoms.iter().flat_map(SelectorAtom::operands) {
            match operand {
                SelectorOperand::Variable { .. } => {}
                SelectorOperand::SourceUnitConstant(source_unit) => {
                    self.add_source_unit(source_unit)
                }
                SelectorOperand::StringConstant(value) => self.add_string(value),
                SelectorOperand::ProjectedAllowedTuples { rows, .. } => {
                    for value in rows.iter().flatten() {
                        self.add_projected_value(value);
                    }
                }
                SelectorOperand::ExcludedSourceUnits(excluded) => {
                    self.source_units.extend(excluded.iter().copied());
                }
            }
        }
    }

    fn add_projected_value(&mut self, value: &SelectorProjectedValue) {
        match value {
            SelectorProjectedValue::SourceUnit(value) => self.add_source_unit(*value),
            SelectorProjectedValue::String(value) => self.add_string(value),
        }
    }

    fn add_source_unit(&mut self, source_unit: SourceUnitId) {
        self.source_units.insert(source_unit);
    }

    fn add_string(&mut self, value: &str) {
        self.strings.insert(value.to_string());
    }
}
