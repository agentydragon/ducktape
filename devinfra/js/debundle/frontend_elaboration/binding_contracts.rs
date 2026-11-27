use super::MemberClaimProjection;
use analysis_vocabulary::inputs::AnalysisAssumptions;
use anyhow::{Result, bail};
use binding_targets::binding_names;
use source_identity::StatementId;
use spec::ChunkRenames;
use spec_resolution::LogicalRequest;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_ecma_ast::{Module, ModuleItem};

pub(super) fn collect_binding_contracts(
    requests: &[LogicalRequest],
    member_claims: &MemberClaimProjection,
    chunk_renames: Option<&ChunkRenames>,
    module: &Module,
    fail_fast: bool,
    metrics: &analysis::diagnostics::WorkMetrics,
) -> Result<(
    AnalysisAssumptions,
    BTreeSet<String>,
    BTreeMap<String, spec::InitializerExternalEffects>,
)> {
    let mut assumptions = AnalysisAssumptions::default();
    let mut source_only_initializer_bindings = BTreeSet::new();
    let mut external_initializer_contracts = BTreeMap::new();
    let mut initializer_targets = InitializerTargets {
        module,
        index: None,
        metrics,
    };
    for (request_index, request) in requests.iter().enumerate() {
        for (member_index, member) in request.members.iter().enumerate() {
            if !member.annotation.has_semantic_annotations() {
                continue;
            }
            let binding = if member.binding.is_empty() {
                member_claims
                    .claimed_members
                    .get(&(request_index, member_index))
                    .cloned()
            } else {
                Some(member.binding.clone())
            };
            if let Some(binding) = binding {
                collect_binding_annotation_contracts(
                    &mut assumptions,
                    &mut source_only_initializer_bindings,
                    &mut external_initializer_contracts,
                    &binding,
                    &member.annotation,
                    &mut initializer_targets,
                )?;
            } else if fail_fast {
                bail!(
                    "logical_module {}: semantic annotations on selector member `{}` could not be applied because selector resolution did not produce a concrete binding",
                    request.id,
                    member.export_name
                );
            }
        }
    }
    for (&(request_index, statement_index), &statement) in
        &member_claims.claimed_anonymous_statements
    {
        if let Some(assertion) = requests
            .get(request_index)
            .and_then(|request| request.anonymous_statements.get(statement_index))
            .and_then(|request| request.no_reentry_call.as_ref())
        {
            collect_no_reentry_contract(module, statement, assertion, &mut assumptions)?;
        }
    }
    collect_chunk_rename_binding_contracts(
        chunk_renames,
        &mut initializer_targets,
        &mut assumptions,
        &mut source_only_initializer_bindings,
        &mut external_initializer_contracts,
    )?;
    Ok((
        assumptions,
        source_only_initializer_bindings,
        external_initializer_contracts,
    ))
}

fn collect_no_reentry_contract(
    module: &Module,
    statement: StatementId,
    assertion: &spec::annotations::NoReentryCall,
    assumptions: &mut AnalysisAssumptions,
) -> Result<()> {
    let Some(ModuleItem::Stmt(swc_ecma_ast::Stmt::Expr(expression_statement))) =
        module.body.get(statement.0)
    else {
        return Ok(());
    };
    let Some(call) = asserted_registration_call(&expression_statement.expr) else {
        return Ok(());
    };
    let argument_count = call.args.len();
    if let Some(position) = assertion
        .callback_arguments
        .iter()
        .find(|position| **position >= argument_count)
    {
        bail!(
            "no_reentry_call callback argument position {position} is out of range for the matched call with {argument_count} arguments"
        );
    }
    assumptions.no_reentry_statement_calls.insert(
        (call.span.lo.0, call.span.hi.0),
        spec::RetainedCallbackSignature {
            argument_count,
            callback_arguments: assertion.callback_arguments.iter().copied().collect(),
        },
    );
    Ok(())
}

fn asserted_registration_call(expression: &swc_ecma_ast::Expr) -> Option<&swc_ecma_ast::CallExpr> {
    match expression.unwrap_parens() {
        swc_ecma_ast::Expr::Call(call) => Some(call),
        swc_ecma_ast::Expr::Bin(binary) if binary.op == swc_ecma_ast::BinaryOp::LogicalAnd => {
            asserted_registration_call(&binary.right)
        }
        _ => None,
    }
}

fn collect_chunk_rename_binding_contracts(
    chunk_renames: Option<&ChunkRenames>,
    initializer_targets: &mut InitializerTargets<'_>,
    assumptions: &mut AnalysisAssumptions,
    source_only_initializer_bindings: &mut BTreeSet<String>,
    external_initializer_contracts: &mut BTreeMap<String, spec::InitializerExternalEffects>,
) -> Result<()> {
    let Some(chunk_renames) = chunk_renames else {
        return Ok(());
    };
    let mut by_export_name = BTreeMap::<String, Vec<String>>::new();
    for member in &chunk_renames.members {
        let binding = &member.selector.binding.name;
        let export_name = member.name.as_deref().unwrap_or(binding);
        by_export_name
            .entry(export_name.to_string())
            .or_default()
            .push(binding.clone());
    }
    for (export_name, annotation) in &chunk_renames.annotations {
        let Some(bindings) = by_export_name.get(export_name) else {
            bail!(
                "chunk_renames.annotations key `{export_name}` does not match any chunk_renames member name"
            );
        };
        if bindings.len() != 1 {
            bail!(
                "chunk_renames.annotations key `{export_name}` is ambiguous because that readable binding name is claimed {} times",
                bindings.len()
            );
        }
        collect_binding_annotation_contracts(
            assumptions,
            source_only_initializer_bindings,
            external_initializer_contracts,
            &bindings[0],
            annotation,
            initializer_targets,
        )?;
    }
    Ok(())
}

fn collect_binding_annotation_contracts(
    assumptions: &mut AnalysisAssumptions,
    source_only_initializer_bindings: &mut BTreeSet<String>,
    external_initializer_contracts: &mut BTreeMap<String, spec::InitializerExternalEffects>,
    binding: &str,
    annotation: &spec::annotations::BindingAnnotation,
    initializer_targets: &mut InitializerTargets<'_>,
) -> Result<()> {
    if annotation.retained_record_factory {
        assumptions
            .retained_record_factories
            .insert(binding.to_string());
    }
    if annotation.initializer_purity == spec::InitializerPurity::Pure {
        validate_initializer_target(initializer_targets, binding, "initializer_purity")?;
        assumptions
            .declared_pure_initializers
            .insert(binding.to_string());
    }
    if annotation.initializer_binding_effects == Some(spec::InitializerBindingEffects::SourceOnly) {
        validate_initializer_target(initializer_targets, binding, "initializer_binding_effects")?;
        source_only_initializer_bindings.insert(binding.to_string());
    }
    if let Some(effects) = annotation.initializer_external_effects {
        validate_initializer_target(initializer_targets, binding, "initializer_external_effects")?;
        if let Some(previous) = external_initializer_contracts.insert(binding.to_string(), effects)
        {
            anyhow::ensure!(
                previous == effects,
                "conflicting initializer_external_effects for `{binding}`: {previous} and {effects}"
            );
        }
    }
    if annotation.purity == spec::MemberPurity::Pure {
        assumptions.declared_pure.insert(binding.to_string());
    }
    if annotation.purity == spec::MemberPurity::PureNew {
        assumptions.declared_pure_new.insert(binding.to_string());
    }
    if !annotation.pure_members.is_empty() {
        assumptions
            .declared_pure_members
            .entry(binding.to_string())
            .or_default()
            .extend(annotation.pure_members.iter().cloned());
    }
    if !annotation.no_sync_callback_members.is_empty() {
        assumptions
            .no_sync_callback_members
            .entry(binding.to_string())
            .or_default()
            .extend(annotation.no_sync_callback_members.iter().cloned());
    }
    if let Some(effect) = spec_resolution::known_effect_from_member_effect(annotation.effect) {
        assumptions
            .known_effects
            .insert(binding.to_string(), effect);
    }
    Ok(())
}

struct InitializerTargets<'a> {
    module: &'a Module,
    index: Option<InitializerTargetIndex>,
    metrics: &'a analysis::diagnostics::WorkMetrics,
}

#[derive(Default)]
struct InitializerTargetCounts {
    initialized_sites: usize,
    unsupported_sites: usize,
    bare_var_sites: usize,
}

/// A per-chunk summary of annotation-eligible top-level initializers.
///
/// This records one entry per canonical source unit: each declarator in a
/// top-level comma-list is independent, including declarations under
/// `export`. That is the same split exposed by `top_level_item_views`, without
/// cloning its declarators just to validate annotations.
#[derive(Default)]
struct InitializerTargetIndex {
    by_binding: HashMap<String, InitializerTargetCounts>,
}

impl InitializerTargetIndex {
    fn new(module: &Module) -> Self {
        let mut index = Self::default();
        for item in &module.body {
            if let Some(var) = top_level_var_declaration(item) {
                for declarator in &var.decls {
                    if let swc_ecma_ast::Pat::Ident(ident) = &declarator.name {
                        let counts = index
                            .by_binding
                            .entry(ident.id.sym.to_string())
                            .or_default();
                        if declarator.init.is_some() {
                            counts.initialized_sites += 1;
                        } else if var.kind == swc_ecma_ast::VarDeclKind::Var {
                            counts.bare_var_sites += 1;
                        } else {
                            counts.unsupported_sites += 1;
                        }
                    } else {
                        for (name, _) in binding_names(&declarator.name) {
                            Self::record_unsupported(&mut index, name.as_ref());
                        }
                    }
                }
                continue;
            }

            match item {
                ModuleItem::ModuleDecl(swc_ecma_ast::ModuleDecl::Import(import)) => {
                    for specifier in &import.specifiers {
                        Self::record_unsupported(&mut index, specifier.local().sym.as_ref());
                    }
                }
                ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(swc_ecma_ast::Decl::Fn(function))) => {
                    Self::record_unsupported(&mut index, function.ident.sym.as_ref());
                }
                ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(swc_ecma_ast::Decl::Class(class))) => {
                    Self::record_unsupported(&mut index, class.ident.sym.as_ref());
                }
                ModuleItem::ModuleDecl(swc_ecma_ast::ModuleDecl::ExportDecl(export)) => {
                    Self::record_exported_nonvariable(&mut index, &export.decl);
                }
                ModuleItem::ModuleDecl(swc_ecma_ast::ModuleDecl::ExportDefaultDecl(export)) => {
                    match &export.decl {
                        swc_ecma_ast::DefaultDecl::Fn(function) => {
                            if let Some(ident) = &function.ident {
                                Self::record_unsupported(&mut index, ident.sym.as_ref());
                            }
                        }
                        swc_ecma_ast::DefaultDecl::Class(class) => {
                            if let Some(ident) = &class.ident {
                                Self::record_unsupported(&mut index, ident.sym.as_ref());
                            }
                        }
                        _ => {}
                    }
                }
                _ => {}
            }
        }
        index
    }

    fn record_exported_nonvariable(index: &mut Self, declaration: &swc_ecma_ast::Decl) {
        match declaration {
            swc_ecma_ast::Decl::Fn(function) => {
                Self::record_unsupported(index, function.ident.sym.as_ref());
            }
            swc_ecma_ast::Decl::Class(class) => {
                Self::record_unsupported(index, class.ident.sym.as_ref());
            }
            _ => {}
        }
    }

    fn record_unsupported(index: &mut Self, binding: &str) {
        index
            .by_binding
            .entry(binding.to_owned())
            .or_default()
            .unsupported_sites += 1;
    }
}

fn top_level_var_declaration(item: &ModuleItem) -> Option<&swc_ecma_ast::VarDecl> {
    match item {
        ModuleItem::Stmt(swc_ecma_ast::Stmt::Decl(swc_ecma_ast::Decl::Var(var))) => {
            Some(var.as_ref())
        }
        ModuleItem::ModuleDecl(swc_ecma_ast::ModuleDecl::ExportDecl(export)) => {
            match &export.decl {
                swc_ecma_ast::Decl::Var(var) => Some(var.as_ref()),
                _ => None,
            }
        }
        _ => None,
    }
}

fn validate_initializer_target(
    targets: &mut InitializerTargets<'_>,
    binding: &str,
    contract: &str,
) -> Result<()> {
    targets.metrics.initializer_validation();
    let index = targets
        .index
        .get_or_insert_with(|| InitializerTargetIndex::new(targets.module));
    let counts = index.by_binding.get(binding);
    let initialized_sites = counts.map_or(0, |counts| counts.initialized_sites);
    let unsupported_sites = counts.map_or(0, |counts| counts.unsupported_sites);
    let bare_var_sites = counts.map_or(0, |counts| counts.bare_var_sites);
    if initialized_sites == 1 && unsupported_sites == 0 {
        return Ok(());
    }
    let detail = if initialized_sites > 1 {
        "multiple initialized declarations"
    } else if unsupported_sites > 0 {
        "a non-simple, imported, or non-variable declaration"
    } else if bare_var_sites > 0 && initialized_sites == 0 {
        "no initialized declaration"
    } else {
        "no simple initialized variable declaration"
    };
    bail!(
        "{contract} for binding `{binding}` requires exactly one direct top-level simple variable initializer; found {detail}"
    )
}
