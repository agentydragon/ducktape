//! Rewrite of `selector.binding.name` members into proven `source_matches[]` claims.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::{Result, bail};
use serde_yaml::Value;
use synthesis_failure::SynthesisSkipReason;

use crate::chunk_index::ChunkSelectorIndex;
use crate::group_synthesis::{
    GroupSelectorOutcome, RequestedDeclGroup, SynthesizedDeclGroup,
    synthesize_requested_declaration_groups, synthesize_simplest_selector_for_group,
};
use crate::module_yaml::{
    MigratedSourceBinding, add_synthesized_member_annotations, apply_member_replacements,
    ensure_optional_sequence, existing_annotations_mapping, mapping_get, mapping_get_path,
    source_match_claim_value, synthesized_member_annotation_fields,
    synthesized_source_match_claim_value, value_as_string, yk,
};
use crate::report::{
    SelectorCodemodCandidate, SynthesizedCandidateInput, skipped_candidate, synthesized_candidate,
};
use crate::shape_runs::merge_adjacent_same_shape_runs;

#[derive(Debug)]
pub(crate) struct NameBindingRewriteOutcomes {
    pub(crate) candidates: Vec<SelectorCodemodCandidate>,
    pub(crate) members_scanned: usize,
    pub(crate) members_seen: usize,
    pub(crate) groups_changed: usize,
}

#[derive(Debug, Clone)]
pub(crate) struct NameBindingMember {
    pub(crate) member_index: usize,
    pub(crate) export_name: String,
    pub(crate) binding_name: String,
    pub(crate) annotation_fields: Vec<(&'static str, Value)>,
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct NameBindingRewriteOptions {
    pub(crate) apply: bool,
    pub(crate) candidates: usize,
}

pub(crate) fn rewrite_name_bindings_to_source_match(
    module: &str,
    file: &Path,
    root: &mut serde_yaml::Mapping,
    index: &ChunkSelectorIndex<'_>,
    selected_exports: Option<&BTreeSet<String>>,
    options: NameBindingRewriteOptions,
) -> Result<NameBindingRewriteOutcomes> {
    ensure_optional_sequence(root, "source_matches", module)?;
    let original_annotations = existing_annotations_mapping(root, module)?;
    let mut annotations = original_annotations.clone();
    let Some(Value::Sequence(members)) = root.get_mut(yk("members")) else {
        return Ok(NameBindingRewriteOutcomes {
            candidates: Vec::new(),
            members_scanned: 0,
            members_seen: 0,
            groups_changed: 0,
        });
    };
    let mut candidates = Vec::new();
    let mut grouped: BTreeMap<usize, Vec<NameBindingMember>> = BTreeMap::new();
    let mut remaining_exports = selected_exports.cloned();
    let mut members_scanned = 0;
    let mut members_seen = 0;

    for (member_index, member) in members.iter().enumerate() {
        if remaining_exports.as_ref().is_some_and(BTreeSet::is_empty) {
            break;
        }
        members_scanned += 1;
        let export_name = mapping_get(member, "name").and_then(value_as_string);
        let Some(export_name) = export_name else {
            continue;
        };
        if let Some(selected_exports) = selected_exports {
            if !selected_exports.contains(&export_name) {
                continue;
            }
            if let Some(remaining_exports) = &mut remaining_exports {
                remaining_exports.remove(&export_name);
            }
        }
        let Some(binding_name) =
            mapping_get_path(member, &["selector", "binding", "name"]).and_then(value_as_string)
        else {
            continue;
        };
        members_seen += 1;
        let decl_indices = index
            .binding_to_decl
            .get(&binding_name)
            .cloned()
            .unwrap_or_default();
        let [decl_idx] = decl_indices.as_slice() else {
            let reason = SynthesisSkipReason::BindingDeclarationCount {
                binding: binding_name.clone(),
                count: decl_indices.len(),
            };
            candidates.push(skipped_candidate(
                module,
                file,
                member_index,
                Some(export_name),
                reason,
            ));
            continue;
        };
        grouped
            .entry(*decl_idx)
            .or_default()
            .push(NameBindingMember {
                member_index,
                export_name,
                binding_name,
                annotation_fields: synthesized_member_annotation_fields(member),
            });
    }

    // Synthesize each declaration group; collect the successes for the grouping
    // pass and emit skip/error candidates immediately (they carry no selector to
    // group).
    let mut synthesized_groups = Vec::new();
    for RequestedDeclGroup {
        decl_idx,
        members: group_members,
        outcome,
    } in synthesize_requested_declaration_groups(index, grouped, options.candidates)?
    {
        // Some groups still have no proving joint selector. Salvage members
        // whose own declarators prove as separate claims.
        if group_members.len() > 1 && !matches!(&outcome, Ok(GroupSelectorOutcome::Synthesized(_)))
        {
            for member in group_members {
                let only_member = vec![member];
                match synthesize_simplest_selector_for_group(
                    index,
                    decl_idx,
                    &only_member,
                    options.candidates,
                ) {
                    Ok(GroupSelectorOutcome::Synthesized(synthesized)) => {
                        synthesized_groups.push(SynthesizedDeclGroup {
                            decl_idx,
                            members: only_member,
                            synthesized,
                        });
                    }
                    Ok(GroupSelectorOutcome::Skipped(reason)) => {
                        candidates.push(skipped_candidate(
                            module,
                            file,
                            only_member[0].member_index,
                            Some(only_member[0].export_name.clone()),
                            reason,
                        ))
                    }
                    Err(error) => return Err(error),
                }
            }
            continue;
        }
        match outcome {
            Ok(GroupSelectorOutcome::Synthesized(synthesized)) => {
                synthesized_groups.push(SynthesizedDeclGroup {
                    decl_idx,
                    members: group_members,
                    synthesized,
                });
            }
            Ok(GroupSelectorOutcome::Skipped(reason)) => {
                for member in group_members {
                    candidates.push(skipped_candidate(
                        module,
                        file,
                        member.member_index,
                        Some(member.export_name),
                        reason.clone(),
                    ));
                }
            }
            Err(error) => return Err(error),
        }
    }

    // Anti-unification grouping: collapse
    // maximal runs of adjacent, same-shape single-target declarations (any kind:
    // function, class, var) into one run-based source_matches[] claim. Multi-declarator var
    // groups (already grouped by shared declaration) and lone groups pass through
    // unchanged.
    let prepared_groups = merge_adjacent_same_shape_runs(index, synthesized_groups);

    // Successful rewrites become canonical `source_matches[]` claims. Rewritten
    // members are removed from `members`, and explicit member note/comment fields
    // move into module-level `annotations`.
    let mut replacements: BTreeMap<usize, Option<Value>> = BTreeMap::new();
    let mut source_matches = Vec::new();
    let groups_changed = prepared_groups.len();
    for (group_id, prepared) in prepared_groups.into_iter().enumerate() {
        let SynthesizedDeclGroup {
            members: group_members,
            synthesized,
            ..
        } = prepared;
        if group_members.len() == 1 {
            let member = &group_members[0];
            let target = &synthesized.target_bindings[0];
            candidates.push(synthesized_candidate(SynthesizedCandidateInput {
                module,
                file,
                member_index: member.member_index,
                export_name: Some(member.export_name.clone()),
                apply: options.apply,
                group_id,
                synthesized: &synthesized,
                target_binding: Some(target.selector_local.clone()),
            }));
            if options.apply {
                add_synthesized_member_annotations(module, member, &mut annotations)?;
                source_matches.push(source_match_claim_value(
                    &synthesized.match_source,
                    vec![MigratedSourceBinding {
                        local: target.selector_local.clone(),
                        name: member.export_name.clone(),
                    }],
                ));
                replacements.insert(member.member_index, None);
            }
        } else {
            for member in &group_members {
                candidates.push(synthesized_candidate(SynthesizedCandidateInput {
                    module,
                    file,
                    member_index: member.member_index,
                    export_name: Some(member.export_name.clone()),
                    apply: options.apply,
                    group_id,
                    synthesized: &synthesized,
                    target_binding: None,
                }));
                if options.apply {
                    add_synthesized_member_annotations(module, member, &mut annotations)?;
                    replacements.insert(member.member_index, None);
                }
            }
            if options.apply {
                source_matches.push(synthesized_source_match_claim_value(&synthesized));
            }
        }
    }

    if options.apply {
        apply_member_replacements(members, replacements);
        if !source_matches.is_empty() {
            let entry = root
                .entry(yk("source_matches"))
                .or_insert_with(|| Value::Sequence(Vec::new()));
            match entry {
                Value::Sequence(existing) => existing.extend(source_matches),
                _ => bail!("{module}: source_matches exists but is not a sequence"),
            }
        }
        if annotations != original_annotations {
            root.insert(yk("annotations"), Value::Mapping(annotations));
        }
    }

    Ok(NameBindingRewriteOutcomes {
        candidates,
        members_scanned,
        members_seen,
        groups_changed,
    })
}
