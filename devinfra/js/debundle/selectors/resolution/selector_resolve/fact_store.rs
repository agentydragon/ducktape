//! The chunk fact store the selector program's tables are built from.

use std::collections::HashMap;

use analysis::facts::statement_facts::StructuralChunkSourceFacts;
use chunk_facts::{RelationalFactNeeds, extract_relational_facts};
use selector_ir::{SelectorAtom, SelectorFact, SelectorFactStore, SelectorProgram};
use source_identity::SourceUnitId;

use crate::Chunk;

/// The chunk facts `program`'s tables are built from: every statement's source unit,
/// kind and declared bindings, its references to other statements' bindings,
/// and the relation facts only the relational selectors in `program` read.
pub(crate) fn selector_fact_store(
    program: &SelectorProgram,
    chunk: &Chunk<'_>,
) -> SelectorFactStore {
    let mut store = SelectorFactStore::default();
    push_statement_facts(&mut store, &chunk.structural);
    push_reference_facts(&mut store, &chunk.structural);
    push_relational_facts(&mut store, program, chunk);
    store
}

/// Every statement's source unit and kind, and the bindings it declares.
fn push_statement_facts(
    store: &mut SelectorFactStore,
    structural: &StructuralChunkSourceFacts<'_>,
) {
    for statement in &structural.per_statement {
        let source_unit = SourceUnitId(statement.ordinal.0);
        store.push(SelectorFact::SourceUnit {
            source_unit,
            statement_ordinal: statement.ordinal,
            statement_kind: statement.kind.to_string(),
        });
        for binding in &statement.declared {
            store.push(SelectorFact::DeclaredBinding {
                source_unit,
                binding: binding.0.as_str().to_string(),
            });
        }
    }
}

/// Every statement's references to bindings another statement declares.
fn push_reference_facts(
    store: &mut SelectorFactStore,
    structural: &StructuralChunkSourceFacts<'_>,
) {
    // A repeated `var` is one selector binding with several syntactic
    // declaration sites. Use its first source-order source unit for binding lookup,
    // matching the dependency graph's representative after CoDeclaration edges
    // have made the sites inseparable. Relational facts remain attached
    // to their actual statement source units, because different bindings declared
    // in one statement can have different repeated-site histories.
    let mut binding_source_unit = HashMap::<swc_ecma_ast::Id, SourceUnitId>::new();
    for statement in &structural.per_statement {
        for binding in &statement.declared {
            binding_source_unit
                .entry(binding.clone())
                .or_insert(SourceUnitId(statement.ordinal.0));
        }
    }
    for statement in &structural.per_statement {
        let source_unit = SourceUnitId(statement.ordinal.0);
        let references = statement
            .reads
            .eager
            .iter()
            .chain(statement.reads.lazy.iter())
            .chain(statement.rebinds.eager.iter())
            .chain(statement.rebinds.first_order_lazy.iter())
            .chain(
                statement
                    .rebinds
                    .lazy
                    .iter()
                    .filter(|binding| !statement.rebinds.first_order_lazy.contains(*binding)),
            );
        for binding in references {
            let Some(&target_source_unit) = binding_source_unit.get(binding) else {
                continue;
            };
            if target_source_unit != source_unit {
                store.push(SelectorFact::SourceUnitReferencesBinding {
                    source_unit,
                    binding: binding.0.as_str().to_string(),
                });
            }
        }
    }
}

/// The relation facts the relational selectors in `program` read, and only those.
fn push_relational_facts(
    store: &mut SelectorFactStore,
    program: &SelectorProgram,
    chunk: &Chunk<'_>,
) {
    let mut reads_member = false;
    let mut consumes_module_member = false;
    let mut needs = RelationalFactNeeds::default();
    for atom in &program.atoms {
        match atom {
            SelectorAtom::ReadsMember { .. } | SelectorAtom::ReadsMemberOfSourceUnit { .. } => {
                reads_member = true;
            }
            SelectorAtom::ConsumesModuleMember { .. } => consumes_module_member = true,
            SelectorAtom::PassedToCall { .. } | SelectorAtom::PassedToCallOfSourceUnit { .. } => {
                needs.call_arguments = true;
            }
            SelectorAtom::MakesDecorateCallForSourceUnit { .. } => needs.decorate_calls = true,
            SelectorAtom::IntrinsicAlias { .. } => needs.intrinsic_aliases = true,
            _ => {}
        }
    }
    needs.member_reads = reads_member || consumes_module_member;
    let facts = extract_relational_facts(chunk.module, needs);
    let imports = consumes_module_member.then(|| &chunk.places().imports);
    for (statement_ordinal, read) in facts.member_reads {
        if let Some(import) = imports.and_then(|imports| imports.get(read.object.as_ref()?)) {
            store.push(SelectorFact::ModuleMemberUse {
                statement_ordinal,
                module: import.source.clone(),
                member: read.member.clone(),
            });
        }
        if reads_member {
            store.push(SelectorFact::MemberRead {
                statement_ordinal,
                object: read.object,
                member: read.member,
            });
        }
    }
    for call in facts.call_arguments {
        store.push(SelectorFact::CallArgumentUse {
            argument: call.argument,
            callee_object: call.callee_object,
            callee_member: call.callee_member,
            arg_index: call.arg_index,
        });
    }
    for call in facts.decorate_calls {
        store.push(SelectorFact::DecorateCallUse {
            callee: call.callee,
            class_anchor: call.class_anchor,
            member: call.member,
        });
    }
    for alias in facts.intrinsic_aliases {
        store.push(SelectorFact::IntrinsicAliasUse {
            binding: alias.binding,
            property: alias.property,
        });
    }
}
