//! Placement-only resolution: frozen base anchors and the overlay resolve against them.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail, ensure};
use selector_outcome::Outcome;
use source_identity::{SourceUnitId, SourceUnitOrdinal};
use spec::member_selectors::{BindingSelector, MemberSelectorSpec};

use crate::projection::Resolve;
use crate::{Chunk, EntityIndex, Member, MemberSelector, Resolution, SpecModule, solve};

/// The frozen base holds one member per logical export identity, and every path
/// that extends it refuses a second claim on an identity with this sentence.
fn duplicate_export_identity(claim: &str, module: &str, export_name: &str) -> String {
    format!("{claim} contains duplicate export identity {module}::{export_name}")
}

/// The named exports of a fully resolved base assignment, frozen as
/// read-only reference anchors for a separate placement-only resolution.
/// Source-unit ids are transient solver inputs; this type is not serialized.
#[derive(Debug, Clone, Default)]
pub struct PlacementAnchors {
    modules: Vec<PlacementAnchorModule>,
}

/// A base export backed by a validated named-import pin. Imports have a
/// binding identity for source-match references, but no local source unit for
/// relational selectors to target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrozenImportAnchor {
    pub logical_module: String,
    pub export_name: String,
    pub imported_binding: String,
}

#[derive(Debug, Clone, Default)]
struct PlacementAnchorModule {
    path: String,
    members: Vec<PlacementAnchor>,
}

#[derive(Debug, Clone)]
struct PlacementAnchor {
    export_name: String,
    /// Binding name used by free-identifier references. An unresolved base
    /// outcome is rejected while freezing and cannot become an anchor.
    binding: String,
    /// Canonical source unit for relational selectors. Imports have no local
    /// source-unit anchor.
    source_unit: Option<SourceUnitId>,
}

#[derive(Debug, Clone)]
pub(crate) struct FrozenPlacementAnchor {
    pub(crate) binding: String,
    pub(crate) source_unit: Option<SourceUnitId>,
}

impl<'m> Chunk<'m> {
    /// Freezes the canonical placements in `resolution` without resolving
    /// any base selector again.
    pub fn freeze_placement_anchors(
        &self,
        modules: &[SpecModule],
        resolution: &Resolution,
    ) -> Result<PlacementAnchors> {
        let places = self.places();
        let mut module_paths = BTreeSet::new();
        let mut export_identities = BTreeSet::new();
        let mut frozen_modules = Vec::with_capacity(modules.len());
        for (module_index, module) in modules.iter().enumerate() {
            ensure!(
                module_paths.insert(module.path.as_str()),
                "frozen base contains duplicate logical module path {:?}",
                module.path
            );
            let mut frozen_members = Vec::with_capacity(module.members.len());
            for (member_index, member) in module.members.iter().enumerate() {
                ensure!(
                    export_identities.insert((module.path.as_str(), member.export_name.as_str())),
                    "{}",
                    duplicate_export_identity("frozen base", &module.path, &member.export_name)
                );
                let outcome = resolution
                    .outcome(module_index, EntityIndex::Member(member_index))
                    .with_context(|| {
                        format!(
                            "canonical base resolution has no outcome for {}::{}",
                            module.path, member.export_name
                        )
                    })?;
                let Outcome::Resolved {
                    body_index,
                    binding: Some(binding),
                    ..
                } = &outcome.outcome
                else {
                    bail!(
                        "canonical base export {}::{} is not resolved to one binding: {:?}",
                        module.path,
                        member.export_name,
                        outcome.outcome
                    );
                };
                let source_unit = places
                    .declarations
                    .get(binding)
                    .and_then(|declarations| declarations.first())
                    .map(|(source_unit, _, _)| *source_unit)
                    .with_context(|| {
                        format!(
                            "resolved base export {}::{} has no canonical declaration",
                            module.path, member.export_name
                        )
                    })?;
                let matched_source_unit = places
                    .source_unit_by_body_and_binding
                    .get(&(*body_index, binding.clone()))
                    .copied()
                    .with_context(|| {
                        format!(
                            "resolved base export {}::{} has no matched declaration",
                            module.path, member.export_name
                        )
                    })?;
                if !places
                    .declarations
                    .get(binding)
                    .is_some_and(|declarations| {
                        declarations
                            .iter()
                            .any(|(unit, _, _)| *unit == matched_source_unit)
                    })
                {
                    bail!(
                        "resolved base export {}::{} has inconsistent declaration identity",
                        module.path,
                        member.export_name
                    );
                }
                frozen_members.push(PlacementAnchor {
                    export_name: member.export_name.clone(),
                    binding: binding.clone(),
                    source_unit: Some(source_unit),
                });
            }
            frozen_modules.push(PlacementAnchorModule {
                path: module.path.clone(),
                members: frozen_members,
            });
        }
        Ok(PlacementAnchors {
            modules: frozen_modules,
        })
    }

    /// Adds already-validated named-import pins as binding-only base anchors.
    /// The caller derives these from the complete request after import
    /// validation; this method checks that each binding is an import in this
    /// exact source chunk and that its export identity is unique in the module.
    pub fn add_frozen_import_anchors(
        &self,
        anchors: &mut PlacementAnchors,
        imports: &[FrozenImportAnchor],
    ) -> Result<()> {
        let imported_bindings = &self.places().imports;
        let mut identities = BTreeSet::new();
        for import in imports {
            ensure!(
                identities.insert((import.logical_module.as_str(), import.export_name.as_str())),
                "{}",
                duplicate_export_identity(
                    "frozen import anchors",
                    &import.logical_module,
                    &import.export_name,
                )
            );
            ensure!(
                imported_bindings.contains_key(&import.imported_binding),
                "frozen import anchor {}::{} names non-import binding {:?}",
                import.logical_module,
                import.export_name,
                import.imported_binding
            );
            let module = anchors
                .modules
                .iter_mut()
                .find(|module| module.path == import.logical_module)
                .with_context(|| {
                    format!(
                        "frozen import anchor names unknown base module {:?}",
                        import.logical_module
                    )
                })?;
            ensure!(
                !module
                    .members
                    .iter()
                    .any(|member| member.export_name == import.export_name),
                "{}",
                duplicate_export_identity(
                    "frozen base",
                    &import.logical_module,
                    &import.export_name,
                )
            );
        }
        for import in imports {
            let module = anchors
                .modules
                .iter_mut()
                .find(|module| module.path == import.logical_module)
                .expect("the validation pass found the import anchor's module");
            module.members.push(PlacementAnchor {
                export_name: import.export_name.clone(),
                binding: import.imported_binding.clone(),
                source_unit: None,
            });
        }
        Ok(())
    }

    /// Resolves only the overlay selectors. Frozen base exports participate
    /// in canonical name/reference resolution, while occupied source units
    /// are excluded from every overlay target's domain.
    pub fn resolve_placement(
        &self,
        overlay_modules: &[SpecModule],
        anchors: &PlacementAnchors,
        occupied_source_units: &BTreeSet<SourceUnitOrdinal>,
    ) -> Result<Resolution> {
        let frozen_paths = anchors
            .modules
            .iter()
            .map(|module| module.path.as_str())
            .collect::<BTreeSet<_>>();
        let mut overlay_paths = BTreeSet::new();
        for module in overlay_modules {
            ensure!(
                overlay_paths.insert(module.path.as_str()),
                "placement overlay contains duplicate logical module path {:?}",
                module.path
            );
            if frozen_paths.contains(module.path.as_str()) {
                bail!(
                    "placement overlay module path {:?} already exists in the frozen base",
                    module.path
                );
            }
        }
        let known_source_ordinals = self
            .structural
            .per_statement
            .iter()
            .map(|statement| statement.ordinal)
            .collect::<BTreeSet<_>>();
        if let Some(unknown) = occupied_source_units
            .difference(&known_source_ordinals)
            .next()
        {
            bail!(
                "occupied source-unit ordinal {unknown:?} is not present in chunk {}",
                self.name
            );
        }
        let occupied_source_units = occupied_source_units
            .iter()
            .map(|ordinal| SourceUnitId(ordinal.0))
            .collect();

        let mut modules = anchors
            .modules
            .iter()
            .map(|module| SpecModule {
                path: module.path.clone(),
                members: Vec::new(),
                anonymous_statements: Vec::new(),
            })
            .collect::<Vec<_>>();
        let mut frozen_anchor_members = BTreeMap::new();
        for (module_index, anchor_module) in anchors.modules.iter().enumerate() {
            for anchor in &anchor_module.members {
                let member_index = modules[module_index].members.len();
                modules[module_index].members.push(Member {
                    export_name: anchor.export_name.clone(),
                    selector: MemberSelector::Authored(MemberSelectorSpec::Binding(
                        BindingSelector {
                            name: anchor.binding.clone(),
                            kind: None,
                        },
                    )),
                });
                frozen_anchor_members.insert(
                    (module_index, member_index),
                    FrozenPlacementAnchor {
                        binding: anchor.binding.clone(),
                        source_unit: anchor.source_unit,
                    },
                );
            }
        }
        let frozen_module_count = modules.len();
        modules.extend_from_slice(overlay_modules);
        let projection = Resolve::new_placement(
            self,
            &modules,
            frozen_module_count,
            frozen_anchor_members,
            occupied_source_units,
        )
        .project()?;
        let [mut resolution] = solve(vec![(self, modules.as_slice(), projection)])?
            .try_into()
            .unwrap_or_else(|_| unreachable!("one placement query resolves to one result"));
        for outcome in &mut resolution.outcomes {
            debug_assert!(outcome.module >= frozen_module_count);
            outcome.module -= frozen_module_count;
        }
        for reference in &mut resolution.member_references {
            debug_assert!(reference.source_module >= frozen_module_count);
            reference.source_module -= frozen_module_count;
            debug_assert!(reference.referenced_module >= frozen_module_count);
            reference.referenced_module -= frozen_module_count;
        }
        Ok(resolution)
    }
}
