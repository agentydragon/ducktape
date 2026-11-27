//! Atomic-group synthesis and owner completion phases of `ChunkPlanBuilder`.

use crate::exports::PublicExportSurface;
use crate::plans::ModulePlan;
use crate::target_paths::target_file_for_request;
use analysis::{BindingKind, EntryDestination, ModuleId, ModuleRole};
use anyhow::{Context, Result, bail};
use esm_schedule::{EsmImportOrder, EsmImportSchedule};
use frontend_elaboration::chunk_ast::SourceExportIdentity;
use graph::assignment_assembly::assemble_assignment_with_source_unit_claims;
use graph::{DependencyGraphAndAtomicGroups, SourceUnitLayout, chunk_constraining_module_edges};
use source_identity::SourceUnitId;
use source_identity::StatementId;
use std::collections::VecDeque;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_ecma_ast::*;

use super::{AtomicGroupOwnerCompletion, ChunkPlanBuilder, owned_destination};

struct CoarseResidualBucket {
    source_units: BTreeSet<SourceUnitId>,
    export_surface: PublicExportSurface,
}

/// Borrow the graph and its layout after checking that every node has a syntax location.
struct PlacementSourceUnits<'a> {
    graph: &'a graph::DependencyGraph,
    layout: &'a SourceUnitLayout,
}

impl<'a> PlacementSourceUnits<'a> {
    fn new(
        precomputed: &'a DependencyGraphAndAtomicGroups,
        layout: &'a SourceUnitLayout,
    ) -> Result<Self> {
        let graph = &precomputed.dependency_graph;
        for node in graph.iter_nodes() {
            layout.checked_location(node.id)?;
        }
        Ok(Self { graph, layout })
    }

    fn declared_names(&self, unit: SourceUnitId) -> &BTreeSet<Id> {
        &self
            .graph
            .node(unit)
            .expect("atomic groups contain graph nodes")
            .declared
    }

    fn statement(&self, unit: SourceUnitId) -> StatementId {
        self.layout
            .by_source_unit(unit)
            .expect("every graph node was checked against the source-unit layout")
            .statement
    }
}

impl ChunkPlanBuilder {
    /// `PerAtomicGroup` mode: every unclaimed atomic factor group gets
    /// its own synthesized plan at `__auto/atomic_groups/NNNN`. Run after the
    /// residual sweep has settled which atomic groups are still unclaimed. Bindings
    /// previously parked at the residual plan are moved out into the
    /// synthesized plan; the residual plan's `bindings` map is
    /// pruned to match.
    pub(in super::super) fn synthesize_per_atomic_group(
        &mut self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        target_dir: &str,
    ) -> Result<()> {
        let groups = self.wholly_unclaimed_atomic_groups(precomputed, layout)?;
        self.synthesize_source_unit_groups(
            groups,
            precomputed,
            layout,
            target_dir,
            "__auto/atomic_groups",
            false,
        )
    }

    /// Complete a partially claimed atomic group only for an explicitly
    /// opted-in authored owner. This runs after ownership subset filtering. Groups with zero or multiple current owners, and
    /// groups whose sole owner is not listed, remain untouched for ordinary
    /// residual routing and the complete-assignment gate.
    pub(in super::super) fn route_partial_atomic_groups_to_named_owners(
        &mut self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        opted_in_owner_paths: &[String],
        single_authored_owner_groups: &BTreeMap<Vec<SourceUnitId>, String>,
    ) -> Result<()> {
        if opted_in_owner_paths.is_empty() {
            return Ok(());
        }

        let source_units = PlacementSourceUnits::new(precomputed, layout)?;

        let opted_in_owner_paths: BTreeSet<String> = opted_in_owner_paths.iter().cloned().collect();
        let mut atomic_groups: Vec<&BTreeSet<SourceUnitId>> = precomputed
            .atomic_groups
            .iter()
            .map(|atomic_group| &atomic_group.members)
            .collect();
        atomic_groups.sort_by_key(|members| members.iter().next().copied());

        for members in atomic_groups {
            let member_key: Vec<_> = members.iter().copied().collect();
            // Ownership subsets must not turn a two-owner authored atom into
            // an eligible one-owner atom by removing one of its owners.
            if !single_authored_owner_groups.contains_key(&member_key) {
                continue;
            }
            let mut current_owners = BTreeSet::<usize>::new();
            let mut unowned_bindings = Vec::<(SourceUnitId, Id)>::new();
            let mut unowned_anonymous = Vec::<(SourceUnitId, StatementId)>::new();
            let mut has_imported_binding = false;

            for &source_unit_id in members {
                let names = source_units.declared_names(source_unit_id);
                if names.is_empty() {
                    let body_index = source_units.statement(source_unit_id);
                    match self.whole_statement_assignment.get(&body_index).copied() {
                        Some(owner) => {
                            current_owners.insert(owner);
                        }
                        None => unowned_anonymous.push((source_unit_id, body_index)),
                    }
                    continue;
                }

                for name in names {
                    match self.bindings_catalogue.get(name) {
                        Some(BindingKind::Owned { module }) => {
                            current_owners.insert(module.0);
                        }
                        Some(BindingKind::Imported { .. }) => {
                            // Imported re-exports are not declarations that
                            // this chunk can adopt into an authored owner.
                            has_imported_binding = true;
                        }
                        None => unowned_bindings.push((source_unit_id, name.clone())),
                    }
                }
            }

            if has_imported_binding || current_owners.len() != 1 {
                continue;
            }
            let owner_index = *current_owners
                .iter()
                .next()
                .expect("the owner count was checked above");
            let Some((owner_role, owner_path)) = self
                .module_plans
                .get(owner_index)
                .map(|plan| (plan.role, plan.target_path.clone()))
            else {
                bail!("atomic group owner index {owner_index} is outside the module plan");
            };
            if owner_role != ModuleRole::Specified
                || !opted_in_owner_paths.contains(&owner_path)
                || single_authored_owner_groups.get(&member_key) != Some(&owner_path)
            {
                continue;
            }
            if unowned_bindings.is_empty() && unowned_anonymous.is_empty() {
                continue;
            }

            let owner_module = ModuleId(owner_index);
            let mut adopted_source_unit_ids = BTreeSet::new();
            for (source_unit_id, binding) in unowned_bindings {
                let name = binding.0.to_string();
                let owner_plan = &mut self.module_plans[owner_index];
                owner_plan
                    .bindings
                    .entry(name.clone())
                    .or_insert_with(|| name.clone());
                self.bindings_catalogue.insert(
                    binding,
                    BindingKind::Owned {
                        module: owner_module,
                    },
                );
                adopted_source_unit_ids.insert(source_unit_id);
            }
            for (source_unit_id, body_index) in unowned_anonymous {
                self.whole_statement_assignment
                    .insert(body_index, owner_index);
                self.module_plans[owner_index]
                    .anonymous_statements
                    .push(body_index);
                adopted_source_unit_ids.insert(source_unit_id);
            }
            let anonymous_statements = &mut self.module_plans[owner_index].anonymous_statements;
            anonymous_statements.sort_unstable();
            anonymous_statements.dedup();

            self.atomic_group_owner_completions
                .push(AtomicGroupOwnerCompletion {
                    owner_path,
                    atomic_group_source_unit_ids: members.iter().copied().collect(),
                    adopted_source_unit_ids: adopted_source_unit_ids.into_iter().collect(),
                });
        }
        Ok(())
    }

    /// Snapshot which atoms had exactly one opted-in authored owner before an
    /// ownership subset can remove plans or claims. Completion itself runs
    /// later, after subset filtering.
    pub(in super::super) fn single_authored_owner_atomic_groups(
        &self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        opted_in_owner_paths: &[String],
    ) -> Result<BTreeMap<Vec<SourceUnitId>, String>> {
        if opted_in_owner_paths.is_empty() {
            return Ok(BTreeMap::new());
        }

        let source_units = PlacementSourceUnits::new(precomputed, layout)?;

        let opted_in_owner_paths: BTreeSet<&str> =
            opted_in_owner_paths.iter().map(String::as_str).collect();
        let mut atomic_groups: Vec<&BTreeSet<SourceUnitId>> = precomputed
            .atomic_groups
            .iter()
            .map(|atomic_group| &atomic_group.members)
            .collect();
        atomic_groups.sort_by_key(|members| members.iter().next().copied());

        let mut eligible_groups = BTreeMap::new();
        for members in atomic_groups {
            let mut current_owners = BTreeSet::<usize>::new();
            let mut has_imported_binding = false;
            for &source_unit_id in members {
                let names = source_units.declared_names(source_unit_id);
                if names.is_empty() {
                    let body_index = source_units.statement(source_unit_id);
                    if let Some(owner) = self.whole_statement_assignment.get(&body_index) {
                        current_owners.insert(*owner);
                    }
                    continue;
                }
                for name in names {
                    match self.bindings_catalogue.get(name) {
                        Some(BindingKind::Owned { module }) => {
                            current_owners.insert(module.0);
                        }
                        Some(BindingKind::Imported { .. }) => has_imported_binding = true,
                        None => {}
                    }
                }
            }

            if has_imported_binding || current_owners.len() != 1 {
                continue;
            }
            let owner_index = *current_owners
                .iter()
                .next()
                .expect("the owner count was checked above");
            let Some(owner_plan) = self.module_plans.get(owner_index) else {
                bail!("atomic group owner index {owner_index} is outside the module plan");
            };
            if owner_plan.role == ModuleRole::Specified
                && opted_in_owner_paths.contains(owner_plan.target_path.as_str())
            {
                eligible_groups.insert(
                    members.iter().copied().collect(),
                    owner_plan.target_path.clone(),
                );
            }
        }
        Ok(eligible_groups)
    }

    /// Coarse residual routing merges wholly-unclaimed atomic groups when
    /// their strict constraining-edge reachability to existing owners agrees
    /// within one interval between fixed owners' fine-plan evaluations. This
    /// signature/interval partition is only a candidate heuristic: alternating
    /// source-order edges across distinct signatures can still form a cycle.
    /// The materializer checks the resulting actual gate SCCs and may contract
    /// a whole SCC only when every member has generated-residual provenance.
    pub(in super::super) fn synthesize_coarse_atomic_groups(
        &mut self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        source_exports_by_statement: &HashMap<StatementId, Vec<SourceExportIdentity>>,
        target_dir: &str,
    ) -> Result<()> {
        let atomic_groups = self.wholly_unclaimed_atomic_groups(precomputed, layout)?;
        if atomic_groups.is_empty() {
            return Ok(());
        }
        let groups = self.coarse_residual_groups(
            precomputed,
            layout,
            &atomic_groups,
            source_exports_by_statement,
        )?;
        self.synthesize_source_unit_groups(
            groups.iter(),
            precomputed,
            layout,
            target_dir,
            "__auto/residual_groups",
            true,
        )
    }

    fn wholly_unclaimed_atomic_groups<'a>(
        &self,
        precomputed: &'a DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
    ) -> Result<Vec<&'a BTreeSet<SourceUnitId>>> {
        let source_units = PlacementSourceUnits::new(precomputed, layout)?;

        let residual_plan_index = self.residual_plan_index;
        let bindings_catalogue = &self.bindings_catalogue;
        let whole_statement_assignment = &self.whole_statement_assignment;
        // An atomic-group member counts as unclaimed iff every declared binding
        // has no owned destination in `bindings_catalogue` or is assigned to
        // the residual plan (if any); anonymous statements must similarly
        // be unassigned or routed via residual. If any member is
        // claimed by an explicit (non-residual) plan, the spec author
        // already named the atomic group's destination — leave the existing
        // claim intact (and let downstream validation flag an
        // atomic group conflict if the claims disagree).
        let is_source_unit_unclaimed = |source_unit: SourceUnitId| -> bool {
            let names = source_units.declared_names(source_unit);
            for id in names {
                match owned_destination(bindings_catalogue, id) {
                    None => continue,
                    Some(idx) if Some(idx) == residual_plan_index => continue,
                    Some(_) => return false,
                }
            }
            if names.is_empty() {
                let body_idx = source_units.statement(source_unit);
                match whole_statement_assignment.get(&body_idx).copied() {
                    None => return true,
                    Some(idx) if Some(idx) == residual_plan_index => return true,
                    Some(_) => return false,
                }
            }
            true
        };

        let mut unclaimed_atomic_groups: Vec<&BTreeSet<SourceUnitId>> = precomputed
            .atomic_groups
            .iter()
            .filter(|atomic_group| {
                atomic_group
                    .members
                    .iter()
                    .copied()
                    .all(is_source_unit_unclaimed)
            })
            .map(|atomic_group| &atomic_group.members)
            .collect();
        // Stable iteration order: smallest SourceUnitId first.
        unclaimed_atomic_groups.sort_by_key(|members| members.iter().next().copied());
        Ok(unclaimed_atomic_groups)
    }

    fn coarse_residual_groups(
        &self,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        atomic_groups: &[&BTreeSet<SourceUnitId>],
        source_exports_by_statement: &HashMap<StatementId, Vec<SourceExportIdentity>>,
    ) -> Result<Vec<BTreeSet<SourceUnitId>>> {
        let dependency_graph = &precomputed.dependency_graph;
        // Match fine lowering's ID domain, including its tie breaks: generated
        // plans follow explicit plans, and entry follows every generated plan.
        let first_virtual_module = self.module_plans.len();
        let entry_module = ModuleId(
            self.module_plans
                .len()
                .checked_add(atomic_groups.len())
                .context("residual entry module ID overflowed")?,
        );
        let mut source_unit_claims = Vec::<(SourceUnitId, ModuleId)>::new();
        for (statement, destination) in &self.whole_statement_assignment {
            for location in layout.for_statement(*statement) {
                source_unit_claims.push((location.source_unit, ModuleId(*destination)));
            }
        }

        let mut virtual_modules = BTreeSet::new();
        let mut candidate_modules =
            Vec::<(ModuleId, &BTreeSet<SourceUnitId>, PublicExportSurface)>::new();
        for (idx, members) in atomic_groups.iter().enumerate() {
            let module_id = ModuleId(
                first_virtual_module
                    .checked_add(idx)
                    .context("temporary residual module IDs overflowed")?,
            );
            virtual_modules.insert(module_id);
            let mut export_surface = PublicExportSurface::default();
            for source_unit in members.iter() {
                let node = dependency_graph.node(*source_unit).with_context(|| {
                    format!("source unit {} has no export surface", source_unit.0)
                })?;
                let location = layout.checked_location(*source_unit)?;
                for id in &node.declared {
                    export_surface.add_binding(id.0.to_string(), id.clone());
                }
                if let Some(exports) = source_exports_by_statement.get(&location.statement) {
                    export_surface.add_source_identities(exports);
                }
                source_unit_claims.push((*source_unit, module_id));
            }
            candidate_modules.push((module_id, *members, export_surface));
        }

        let assignment = assemble_assignment_with_source_unit_claims(
            dependency_graph,
            &precomputed.atomic_groups,
            &self.bindings_catalogue,
            &source_unit_claims,
            EntryDestination::from_module_id(entry_module),
        )?
        .assignment;
        let canonical_edges = chunk_constraining_module_edges(dependency_graph, &assignment);
        // This deterministic layout heuristic chooses coarse buckets only;
        // it is not an accepted schedule and never tries alternate orders.
        // The settled layout is checked against the author's priority later.
        let nodes = (0..=entry_module.0).map(ModuleId).collect();
        let policy = EsmImportOrder::build_for_assignment(
            &canonical_edges,
            dependency_graph,
            &assignment,
            &nodes,
        );
        let evaluation_order = EsmImportSchedule::from_policy(
            entry_module,
            &canonical_edges.i_successors,
            &nodes,
            &policy,
        )
        .post_order()
        .clone();
        let mut successors = BTreeMap::<ModuleId, BTreeSet<ModuleId>>::new();
        for &(from, to) in canonical_edges.edges.keys() {
            successors.entry(from).or_default().insert(to);
        }
        drop(canonical_edges);

        // A destination is fixed when it already owns at least one source
        // unit outside the candidate atomic groups. This includes a retained
        // residual placeholder and includes entry only when entry actually
        // owns units. Empty planned destinations cannot influence reachability.
        let fixed_destinations: BTreeSet<ModuleId> = assignment
            .iter()
            .map(|(_, destination)| destination)
            .filter(|destination| !virtual_modules.contains(destination))
            .collect();
        let mut fixed_positions: Vec<usize> = fixed_destinations
            .iter()
            .filter_map(|destination| evaluation_order.get(destination).copied())
            .collect();
        fixed_positions.sort_unstable();
        let mut groups_by_signature =
            BTreeMap::<(usize, BTreeSet<ModuleId>), Vec<CoarseResidualBucket>>::new();
        let mut groups = Vec::new();
        for (virtual_module, members, export_surface) in candidate_modules {
            // Entry imports every assigned destination, so source-aware
            // simulation includes isolated candidate modules in the same
            // evaluation order as the emitted tree. Every virtual module has
            // at least one assigned source unit and must therefore appear.
            let position = *evaluation_order
                .get(&virtual_module)
                .expect("assigned virtual residual module is imported from entry");
            let interval = fixed_positions.partition_point(|fixed| *fixed < position);
            let mut reachable = BTreeSet::new();
            let mut visited = BTreeSet::from([virtual_module]);
            let mut pending = VecDeque::from([virtual_module]);
            while let Some(from) = pending.pop_front() {
                for to in successors.get(&from).into_iter().flatten() {
                    if fixed_destinations.contains(to) {
                        reachable.insert(*to);
                    }
                    if visited.insert(*to) {
                        pending.push_back(*to);
                    }
                }
            }
            let buckets = groups_by_signature
                .entry((interval, reachable))
                .or_default();
            let existing_bucket = buckets
                .iter_mut()
                .find(|bucket| bucket.export_surface.is_compatible_with(&export_surface));
            if let Some(bucket) = existing_bucket {
                bucket.source_units.extend(members.iter().copied());
                bucket.export_surface.merge(&export_surface);
            } else {
                buckets.push(CoarseResidualBucket {
                    source_units: members.iter().copied().collect(),
                    export_surface,
                });
            }
        }

        groups.extend(
            groups_by_signature
                .into_values()
                .flatten()
                .map(|bucket| bucket.source_units),
        );
        groups.sort_by_key(|members| members.iter().next().copied());
        Ok(groups)
    }

    fn synthesize_source_unit_groups<'a>(
        &mut self,
        groups: impl IntoIterator<Item = &'a BTreeSet<SourceUnitId>>,
        precomputed: &DependencyGraphAndAtomicGroups,
        layout: &SourceUnitLayout,
        target_dir: &str,
        path_prefix: &str,
        generated_residual: bool,
    ) -> Result<()> {
        let source_units = PlacementSourceUnits::new(precomputed, layout)?;

        let residual_plan_index = self.residual_plan_index;
        for (idx, members) in groups.into_iter().enumerate() {
            let synthetic_idx = self.module_plans.len();
            let synthetic_module_id = ModuleId(synthetic_idx);
            let target_path = format!("{path_prefix}/{idx:04}");
            let target_file = target_file_for_request(target_dir, &target_path)?;
            let mut bindings = HashMap::<String, String>::new();
            let mut anonymous_statements = Vec::<StatementId>::new();
            for &source_unit in members {
                let names = source_units.declared_names(source_unit);
                if names.is_empty() {
                    let body_idx = source_units.statement(source_unit);
                    self.whole_statement_assignment
                        .insert(body_idx, synthetic_idx);
                    anonymous_statements.push(body_idx);
                    continue;
                }
                for name in names {
                    let name_str = name.0.to_string();
                    bindings.insert(name_str.clone(), name_str.clone());
                    // Move the binding out of the residual plan (if it
                    // was staged there by the sweep above) into the
                    // synthesized plan. The residual plan's bindings are
                    // pruned so it doesn't double-claim members.
                    if let Some(prev) = owned_destination(&self.bindings_catalogue, name)
                        && Some(prev) == residual_plan_index
                        && let Some(residual_idx) = residual_plan_index
                    {
                        self.module_plans[residual_idx].bindings.remove(&name_str);
                    }
                    self.bindings_catalogue.insert(
                        name.clone(),
                        BindingKind::Owned {
                            module: synthetic_module_id,
                        },
                    );
                }
            }
            anonymous_statements.sort_unstable();
            self.module_plans.push(ModulePlan {
                id: target_path.clone(),
                target_file,
                target_path,
                role: ModuleRole::Unassigned,
                bindings,
                spec_named_bindings: BTreeSet::new(),
                anonymous_statements,
                anonymous_statement_selector_indices: BTreeMap::new(),
                anonymous_binding_statement_origins: BTreeMap::new(),
                anonymous_statement_comments: BTreeMap::new(),
                comment: None,
                binding_comments: BTreeMap::new(),
                binding_claim_origins: BTreeMap::new(),
            });
            if generated_residual {
                self.generated_residual_plans.insert(synthetic_idx);
            }
        }
        Ok(())
    }
}
