//! Collecting each `source_match` entity's candidate rows from the shape matcher.

use std::collections::{BTreeMap, BTreeSet};

use analysis_domain::item_views::StatementKind;
use anyhow::Result;
use selector_outcome::{
    Entity, EntityRef, InvalidSelectorError, MAX_CANDIDATES_PER_SELECTOR, Outcome, ProjectionError,
};
use source_match::{ParsedSourceMatchSelector, template_free_identifiers};
use spec::AnonymousStatementSelector;

use crate::projection::{Place, Resolve};
use crate::{MemberSelector, Places, SpecModule};

/// A `source_match` entity's candidates, collected before any target is
/// declared so that templates can name entities projected after them.
pub(crate) struct Collected {
    pub(crate) module_index: usize,
    pub(crate) shape: CollectedShape,
    /// The template's free identifiers.
    pub(crate) free: BTreeSet<String>,
    pub(crate) rows: Vec<CollectedRow>,
}

pub(crate) enum CollectedShape {
    Member(usize),
    Group(Group),
    /// An anonymous statement, by its position in the module's list.
    Anonymous(usize),
}

impl Collected {
    /// The export each row place is claimed as, in place order.
    pub(crate) fn export_names(&self, modules: &[SpecModule]) -> Vec<String> {
        match &self.shape {
            CollectedShape::Member(member_index) => {
                vec![
                    modules[self.module_index].members[*member_index]
                        .export_name
                        .clone(),
                ]
            }
            CollectedShape::Group(group) => group.exports_by_target.values().cloned().collect(),
            CollectedShape::Anonymous(_) => Vec::new(),
        }
    }

    pub(crate) fn member_indices(&self) -> Vec<usize> {
        match &self.shape {
            CollectedShape::Member(member_index) => vec![*member_index],
            CollectedShape::Group(group) => group.members_by_target.values().copied().collect(),
            CollectedShape::Anonymous(_) => Vec::new(),
        }
    }

    /// The entities it places, as outcomes name them.
    pub(crate) fn entities(&self, modules: &[SpecModule]) -> Vec<Entity> {
        match &self.shape {
            CollectedShape::Anonymous(position) => vec![Entity::AnonymousStatement(
                modules[self.module_index].anonymous_statements[*position].index,
            )],
            _ => self
                .export_names(modules)
                .into_iter()
                .map(Entity::Export)
                .collect(),
        }
    }
}

/// One candidate: a place per target, and what each free identifier bound.
#[derive(Clone)]
pub(crate) struct CollectedRow {
    /// One per target; a member's names its binding, an anonymous
    /// statement's none.
    pub(crate) places: Vec<Place>,
    pub(crate) free_bindings: BTreeMap<String, String>,
}

/// The members of one module that share a `source_match` template and each
/// claim one of its bindings.
#[derive(Debug, Clone)]
pub(crate) struct Group {
    pub(crate) parsed: ParsedSourceMatchSelector,
    pub(crate) exports_by_target: BTreeMap<String, String>,
    pub(crate) members_by_target: BTreeMap<String, usize>,
}

impl Resolve<'_, '_> {
    pub(crate) fn collect_anonymous(
        &self,
        module_index: usize,
        position: usize,
    ) -> Result<Collected, Rejection> {
        let statement = &self.modules[module_index].anonymous_statements[position];
        let places = self.chunk.places();
        let rows = self
            .chunk
            .matcher()
            .anonymous_group_candidates(&self.modules[module_index].path, &statement.selector)
            .map_err(Rejection::matcher)?
            .into_iter()
            .map(|group| {
                let body_idx = group
                    .body_indices
                    .last()
                    .ok_or(ProjectionError::EmptyAnonymousCandidate)?;
                if group.body_indices.len() > 1
                    && places
                        .source_unit_by_body_and_binding
                        .keys()
                        .any(|(index, _)| index == body_idx)
                {
                    return Err(ProjectionError::AnonymousDeclaresBinding {
                        body_index: *body_idx,
                    });
                }
                let source_unit = places.source_unit_by_body.get(body_idx).copied().ok_or(
                    ProjectionError::AnonymousSourceUnitMissing {
                        body_index: *body_idx,
                    },
                )?;
                Ok(CollectedRow {
                    places: vec![Place {
                        source_unit,
                        binding: None,
                    }],
                    free_bindings: group.free_bindings,
                })
            })
            .collect::<Result<Vec<_>, ProjectionError>>()
            .map_err(|error| Rejection::Invalid(error.into()))?;
        Ok(Collected {
            module_index,
            free: template_free_identifiers(&statement.selector),
            shape: CollectedShape::Anonymous(position),
            rows: self.without_occupied_source_units(rows),
        })
    }

    pub(crate) fn collect_group(
        &self,
        module_index: usize,
        group: Group,
    ) -> Result<Collected, Rejection> {
        let (rows, free) =
            self.collect_rows(module_index, &group.parsed, &group.exports_by_target)?;
        Ok(Collected {
            module_index,
            free,
            shape: CollectedShape::Group(group),
            rows,
        })
    }

    /// The candidate rows of `template` (without a target binding) claiming
    /// the locals of `exports_by_target`, one place per local in key order,
    /// and the template's free identifiers that are not claimed. A declared
    /// local's place comes from the matcher. A free local pins by use site:
    /// its place is the top-level declaration its identifier binds to in
    /// that match, one row per declaring statement, and no row when it binds
    /// nothing declared at top level or different identifiers in different
    /// scopes.
    fn collect_rows(
        &self,
        module_index: usize,
        template: &ParsedSourceMatchSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<(Vec<CollectedRow>, BTreeSet<String>), Rejection> {
        let places = self.chunk.places();
        let matcher = self.chunk.matcher();
        let logical_module = &self.modules[module_index].path;
        let declared_names = template
            .declared_binding_names()
            .into_iter()
            .collect::<BTreeSet<_>>();
        let (declared, used): (BTreeMap<_, _>, BTreeMap<_, _>) = exports_by_target
            .iter()
            .map(|(local, export)| (local.clone(), export.clone()))
            .partition(|(local, _)| declared_names.contains(local));
        let matched = if declared.is_empty() {
            matcher
                .free_reference_candidates(logical_module, template)
                .map_err(Rejection::matcher)?
                .into_iter()
                .map(|free_bindings| Ok((BTreeMap::new(), free_bindings)))
                .collect::<Result<Vec<_>, ProjectionError>>()
        } else {
            matcher
                .member_group_candidates(logical_module, template, &declared)
                .map_err(Rejection::matcher)?
                .into_iter()
                .map(|candidate| {
                    Ok((
                        candidate
                            .bindings
                            .into_iter()
                            .map(|(local, matched)| {
                                Ok((
                                    local,
                                    member_place(places, matched.body_idx, matched.binding)?,
                                ))
                            })
                            .collect::<Result<BTreeMap<_, _>, ProjectionError>>()?,
                        candidate.free_bindings,
                    ))
                })
                .collect::<Result<Vec<_>, ProjectionError>>()
        }
        .map_err(|error| Rejection::Invalid(error.into()))?;
        let mut rows = Vec::new();
        for (declared_places, free_bindings) in matched {
            let mut partial = vec![declared_places];
            for local in used.keys() {
                let source_units = free_bindings
                    .get(local)
                    .and_then(|name| {
                        places
                            .declarations
                            .get(name)
                            .map(|source_units| (name, source_units))
                    })
                    .into_iter()
                    .filter_map(|(name, declarations)| {
                        // A repeated `var` has several syntactic declaration
                        // sites but one binding. Use its first source-order
                        // source unit as the placement representative; the source unit
                        // graph co-locates every other site with it.
                        declarations
                            .iter()
                            .find(|(_, _, kind)| *kind != StatementKind::Import)
                            .map(|(source_unit, _, _)| Place {
                                source_unit: *source_unit,
                                binding: Some(name.clone()),
                            })
                    })
                    .collect::<Vec<_>>();
                partial = partial
                    .into_iter()
                    .flat_map(|row| {
                        source_units.iter().map(move |place| {
                            let mut row = row.clone();
                            row.insert(local.clone(), place.clone());
                            row
                        })
                    })
                    .collect();
            }
            rows.extend(partial.into_iter().map(|row| CollectedRow {
                places: row.into_values().collect(),
                free_bindings: free_bindings.clone(),
            }));
        }
        let free = template_free_identifiers(template)
            .into_iter()
            .filter(|name| !used.contains_key(name))
            .collect();
        Ok((self.without_occupied_source_units(rows), free))
    }

    pub(crate) fn collect_member(
        &self,
        module_index: usize,
        member_index: usize,
    ) -> Result<Collected, Rejection> {
        let member = &self.modules[module_index].members[member_index];
        let MemberSelector::SourceMatch(parsed) = &member.selector else {
            unreachable!("only source_match members are collected");
        };
        if let Some(local) = &parsed.selector().target_binding {
            let template = parsed.with_target_binding(None);
            if !template.declared_binding_names().contains(local) {
                let (rows, free) = self.collect_rows(
                    module_index,
                    &template,
                    &BTreeMap::from([(local.clone(), member.export_name.clone())]),
                )?;
                return Ok(Collected {
                    module_index,
                    free,
                    shape: CollectedShape::Member(member_index),
                    rows,
                });
            }
        }
        let places = self.chunk.places();
        let rows = self
            .chunk
            .matcher()
            .member_candidates(&self.modules[module_index].path, parsed)
            .map_err(Rejection::matcher)?
            .into_iter()
            .map(|matched| {
                Ok(CollectedRow {
                    places: vec![member_place(places, matched.body_idx, matched.binding)?],
                    free_bindings: matched.free_bindings,
                })
            })
            .collect::<Result<Vec<_>, ProjectionError>>()
            .map_err(|error| Rejection::Invalid(error.into()))?;
        Ok(Collected {
            module_index,
            free: template_free_identifiers(parsed),
            shape: CollectedShape::Member(member_index),
            rows: self.without_occupied_source_units(rows),
        })
    }

    fn without_occupied_source_units(&self, rows: Vec<CollectedRow>) -> Vec<CollectedRow> {
        rows.into_iter()
            .filter(|row| {
                row.places
                    .iter()
                    .all(|place| !self.occupied_source_units.contains(&place.source_unit))
            })
            .collect()
    }
}

/// Why a `source_match` was rejected before the solve.
#[derive(Debug, Clone)]
pub(crate) enum Rejection {
    NoCandidates,
    /// Its matches all disagree with where these referenced entities are.
    Conflict(Vec<EntityRef>),
    TooBroad(usize),
    Invalid(InvalidSelectorError),
    Operational(source_match::MatcherError),
}

impl Rejection {
    pub(crate) fn matcher(error: source_match::MatcherError) -> Self {
        match error {
            source_match::MatcherError::Invalid(error) => Self::Invalid(error.into()),
            source_match::MatcherError::Invariant { .. } => Self::Operational(error),
        }
    }

    /// Cap distinct placements, not alternative free-name bindings for a place.
    /// Keep all rows: reference columns can still distinguish their bindings.
    pub(crate) fn check_count(rows: Vec<CollectedRow>) -> Result<Vec<CollectedRow>, Self> {
        let count = rows
            .iter()
            .map(|row| &row.places)
            .collect::<BTreeSet<_>>()
            .len();
        match count {
            0 => Err(Self::NoCandidates),
            count if count > MAX_CANDIDATES_PER_SELECTOR => Err(Self::TooBroad(count)),
            _ => Ok(rows),
        }
    }

    pub(crate) fn outcome(self) -> Result<Outcome> {
        Ok(match self {
            Self::NoCandidates => Outcome::no_match(),
            Self::Conflict(with) => Outcome::Conflict { with },
            Self::TooBroad(count) => Outcome::too_broad(count),
            Self::Invalid(error) => Outcome::Invalid { error },
            Self::Operational(error) => return Err(error.into()),
        })
    }
}

pub(crate) fn member_place(
    places: &Places,
    body_idx: usize,
    binding: String,
) -> Result<Place, ProjectionError> {
    let matched_source_unit = places
        .source_unit_by_body_and_binding
        .get(&(body_idx, binding.clone()))
        .copied()
        .ok_or_else(|| ProjectionError::BindingSourceUnitMissing {
            body_index: body_idx,
            binding: binding.clone(),
        })?;
    let source_unit = places
        .declarations
        .get(&binding)
        .and_then(|declarations| declarations.first())
        .map(|(source_unit, _, _)| *source_unit)
        .unwrap_or(matched_source_unit);
    Ok(Place {
        source_unit,
        binding: Some(binding),
    })
}

/// A module's `source_matches[]` members grouped by shared template: members
/// whose templates differ only in their `target_binding` and do not claim the
/// same binding. A one-binding claim keeps group matching semantics after a
/// exact assignment split.
pub(crate) fn source_match_groups(module: &SpecModule) -> Vec<Group> {
    let mut by_template = BTreeMap::<AnonymousStatementSelector, Vec<usize>>::new();
    for (index, member) in module.members.iter().enumerate() {
        let Some(selector) = member.selector.source_match() else {
            continue;
        };
        if selector.target_binding.is_none() {
            continue;
        }
        let mut template = selector.clone();
        template.target_binding = None;
        by_template.entry(template).or_default().push(index);
    }
    let mut groups = by_template
        .into_values()
        .filter_map(|indices| {
            let mut exports_by_target = BTreeMap::new();
            let mut members_by_target = BTreeMap::new();
            for index in &indices {
                let member = &module.members[*index];
                let target = member
                    .selector
                    .source_match()
                    .and_then(|selector| selector.target_binding.clone())
                    .expect("grouped selectors have a target_binding");
                if exports_by_target
                    .insert(target.clone(), member.export_name.clone())
                    .is_some()
                    || members_by_target.insert(target, *index).is_some()
                {
                    return None;
                }
            }
            let MemberSelector::SourceMatch(parsed) = &module.members[indices[0]].selector else {
                unreachable!("grouped members are source_match members");
            };
            Some((
                indices[0],
                Group {
                    parsed: parsed.with_target_binding(None),
                    exports_by_target,
                    members_by_target,
                },
            ))
        })
        .collect::<Vec<_>>();
    groups.sort_by_key(|(first, _)| *first);
    groups.into_iter().map(|(_, group)| group).collect()
}
