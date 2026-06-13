//! Export-list manipulation: trim dead specifiers, reject spec
//! duplicates, auto-grow residual exports for cross-module references,
//! and convert binding maps into `export { ... }` ModuleItems.

use super::*;
use rustc_hash::FxHashSet;
use swc_atoms::Atom;

pub(super) fn trim_dead_named_specifiers(
    body: &mut [ModuleItem],
    bindings: &HashMap<Id, BindingKind>,
) {
    let candidate_syms = claimed_named_import_syms(body, bindings);
    if candidate_syms.is_empty() {
        return;
    }

    let mut collector = TargetedRefCollector::new(&candidate_syms);
    for item in body.iter() {
        if collector.is_complete() {
            break;
        }
        item.visit_with(&mut collector);
    }
    let refs = collector.into_found_syms();
    if refs.len() == candidate_syms.len() {
        return;
    }

    for item in body.iter_mut() {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        // Side-effect-only imports never had specifiers; leave
        // them alone (they exist to evaluate the imported module).
        if import.specifiers.is_empty() {
            continue;
        }
        import.specifiers.retain(|spec| match spec {
            ImportSpecifier::Default(_) | ImportSpecifier::Namespace(_) => true,
            ImportSpecifier::Named(named) => {
                let claimed = candidate_syms.contains(&named.local.sym);
                let unused = !refs.contains(&named.local.sym);
                !(claimed && unused)
            }
        });
        // The directive's `specifiers: vec![]` shape is itself a
        // side-effect-only import — `import "./mod.js";`. Keeping
        // it preserves the source-module evaluation that the
        // original entry depended on, regardless of whether any
        // moved logical module is loaded by the residual.
    }
}

fn claimed_named_import_syms(
    body: &[ModuleItem],
    bindings: &HashMap<Id, BindingKind>,
) -> FxHashSet<Atom> {
    if bindings.is_empty() {
        return FxHashSet::default();
    }
    let claimed_syms: FxHashSet<_> = bindings.keys().map(|id| id.0.clone()).collect();
    let mut candidate_syms = FxHashSet::default();
    for item in body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        for specifier in &import.specifiers {
            if let ImportSpecifier::Named(named) = specifier
                && claimed_syms.contains(&named.local.sym)
            {
                candidate_syms.insert(named.local.sym.clone());
            }
        }
    }
    candidate_syms
}

// Mirrors `RefCollector`'s binding/shadowing rules, but only stores
// symbols that can affect dead import-specifier trimming.
struct TargetedRefCollector<'a> {
    targets: &'a FxHashSet<Atom>,
    found: FxHashSet<Atom>,
    shadowed_scopes: Vec<BTreeSet<String>>,
}

impl<'a> TargetedRefCollector<'a> {
    fn new(targets: &'a FxHashSet<Atom>) -> Self {
        Self {
            targets,
            found: FxHashSet::default(),
            shadowed_scopes: Vec::new(),
        }
    }

    fn is_complete(&self) -> bool {
        self.found.len() == self.targets.len()
    }

    fn into_found_syms(self) -> FxHashSet<Atom> {
        self.found
    }

    fn is_shadowed(&self, name: &str) -> bool {
        self.shadowed_scopes
            .iter()
            .rev()
            .any(|scope| scope.contains(name))
    }

    fn with_shadowed_scope<F: FnOnce(&mut Self)>(&mut self, names: BTreeSet<String>, f: F) {
        self.shadowed_scopes.push(names);
        f(self);
        self.shadowed_scopes.pop();
    }
}

impl Visit for TargetedRefCollector<'_> {
    fn visit_ident(&mut self, node: &Ident) {
        if !self.is_complete()
            && self.targets.contains(&node.sym)
            && !self.is_shadowed(node.sym.as_ref())
        {
            self.found.insert(node.sym.clone());
        }
    }

    fn visit_binding_ident(&mut self, _node: &BindingIdent) {}

    fn visit_import_decl(&mut self, _node: &ImportDecl) {}

    fn visit_function(&mut self, node: &Function) {
        if self.is_complete() {
            return;
        }
        let shadowed = node
            .params
            .iter()
            .flat_map(|param| binding_names(&param.pat))
            .collect::<BTreeSet<_>>();
        self.with_shadowed_scope(shadowed, |collector| node.visit_children_with(collector));
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        if self.is_complete() {
            return;
        }
        let shadowed = node
            .params
            .iter()
            .flat_map(binding_names)
            .collect::<BTreeSet<_>>();
        self.with_shadowed_scope(shadowed, |collector| node.visit_children_with(collector));
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        node.obj.visit_with(self);
        if !self.is_complete()
            && let MemberProp::Computed(computed) = &node.prop
        {
            computed.expr.visit_with(self);
        }
    }

    fn visit_prop_name(&mut self, node: &PropName) {
        if !self.is_complete()
            && let PropName::Computed(computed) = node
        {
            computed.expr.visit_with(self);
        }
    }

    fn visit_jsx_element_name(&mut self, _node: &JSXElementName) {}

    fn visit_jsx_attr_name(&mut self, _node: &JSXAttrName) {}
}

pub(super) fn reject_duplicate_export_names(
    operation: &str,
    id: &str,
    members: &[MemberRequest],
) -> Result<()> {
    reject_duplicate_field(operation, id, "exported logical names", members, |m| {
        &m.export_name
    })
}

pub(super) fn reject_duplicate_member_bindings(
    operation: &str,
    id: &str,
    members: &[MemberRequest],
) -> Result<()> {
    let mut by_binding = BTreeMap::<String, Vec<&MemberRequest>>::new();
    for member in members {
        if member.source_match.is_some() {
            continue;
        }
        by_binding
            .entry(member.binding.clone())
            .or_default()
            .push(member);
    }
    let duplicates = by_binding
        .into_iter()
        .filter(|(_, members)| members.len() > 1)
        .collect::<Vec<_>>();
    if duplicates.is_empty() {
        return Ok(());
    }
    let mut report = format!("{operation} {id} has duplicate source binding claims:");
    for (binding, members) in duplicates {
        let binding = if binding.is_empty() {
            "<unresolved>".to_string()
        } else {
            format!("`{binding}`")
        };
        report.push_str(&format!(
            "\n- source binding {binding} claimed {} times:",
            members.len()
        ));
        for member in members {
            report.push_str(&format!(
                "\n  - export `{}` ({})",
                member.export_name, member.claim_origin
            ));
        }
    }
    bail!("{report}");
}

fn reject_duplicate_field(
    operation: &str,
    id: &str,
    label: &str,
    members: &[MemberRequest],
    extract: impl Fn(&MemberRequest) -> &str,
) -> Result<()> {
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for member in members {
        if member.source_match.is_some() {
            continue;
        }
        let value = extract(member);
        if !seen.insert(value.to_string()) {
            duplicates.insert(value.to_string());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "{operation} {id} has duplicate {label}: {}",
            duplicates.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    Ok(())
}

pub(super) fn export_named_for_bindings(bindings: &BTreeMap<String, String>) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(NamedExport {
        span: DUMMY_SP,
        specifiers: bindings
            .iter()
            .map(|(local, exported)| {
                ExportSpecifier::Named(ExportNamedSpecifier {
                    span: DUMMY_SP,
                    orig: ModuleExportName::Ident(Ident::new_no_ctxt(
                        local.clone().into(),
                        DUMMY_SP,
                    )),
                    exported: if local == exported {
                        None
                    } else {
                        Some(ModuleExportName::Ident(Ident::new_no_ctxt(
                            exported.clone().into(),
                            DUMMY_SP,
                        )))
                    },
                    is_type_only: false,
                })
            })
            .collect(),
        src: None,
        type_only: false,
        with: None,
    }))
}

pub(super) fn entry_exports_for_moved_bindings(
    declarations: &[TopLevelDecl],
    binding_assignment: &HashMap<Id, usize>,
    entry_renames: &BTreeMap<String, String>,
) -> Vec<ModuleItem> {
    let mut exports = BTreeMap::<String, String>::new();
    for decl in declarations.iter().filter(|decl| decl.exported) {
        for (name, id) in &decl.bindings {
            if binding_assignment.contains_key(id) {
                let final_local = entry_renames
                    .get(name)
                    .cloned()
                    .unwrap_or_else(|| name.clone());
                exports.insert(final_local, name.clone());
            }
        }
    }
    if exports.is_empty() {
        Vec::new()
    } else {
        vec![export_named_for_bindings(&exports)]
    }
}

/// Compute the residual entry bindings every moved module body
/// references but entry doesn't yet export. The per-module emit
/// path needs every such reference to import from entry, so the
/// materializer auto-grows entry's export list to cover them — that
/// way peeling a body whose lazy/eager reads target an
/// unexported residual binding emits valid JS without making the
/// peel proposer responsible for predicting the materializer's
/// export policy. See docs/design.md "Valid peels and atomic modules"
/// (importability clause).
///
/// Submits one `EntryPublicExports`-scope intent per grown export,
/// keyed by the residual binding's original name and targeting the
/// minted public name; the caller seals the ledger and maps originals
/// to entry's post-rename locals before feeding
/// `export_named_for_bindings`.
///
/// Skips:
/// - bindings already in `existing_exports` (the upstream source
///   exports plus the moved-binding re-exports already emitted by
///   `entry_exports_for_moved_bindings`),
/// - names not declared anywhere in the chunk
///   (`declaration_by_name` covers every top-level decl, so this is
///   the "globals / runtime imports / unknown ident" case the
///   per-module emit path silently lets fall through to the implicit
///   runtime resolution),
/// - bindings owned by a logical module (`binding_assignment`), which
///   are imported directly module→module rather than mediated by
///   entry.
pub(super) const AUTO_GROWN_EXPORT_CONTRIBUTOR: &str = "auto-grown residual export";

/// The entry-side facts `auto_grown_residual_exports` consults; see
/// `LowerChunkSpecFacts` and `lower_chunk` for the field provenance.
pub(super) struct ExportGrowthFacts<'a> {
    pub(super) declaration_by_name: &'a HashMap<Id, usize>,
    pub(super) binding_assignment: &'a HashMap<Id, usize>,
    pub(super) pre_existing_entry_exports: &'a HashSet<Id>,
    /// ORIGINAL (pre-rename) names declared at the top level of the
    /// post-split entry body. Collection runs before the entry rename
    /// executor, so candidates are checked under their original names;
    /// the seal's injectivity guarantees on the Chunk-scope map make
    /// this equivalent to checking the post-rename declared set.
    pub(super) entry_declared_names: &'a HashSet<String>,
}

pub(super) fn auto_grown_residual_exports(
    body_facts_by_module: &[ModuleBodyFacts],
    facts: &ExportGrowthFacts<'_>,
    chunk_top_level_mark: swc_common::Mark,
    ledger: &mut RenameLedger,
) {
    let &ExportGrowthFacts {
        declaration_by_name,
        binding_assignment,
        pre_existing_entry_exports,
        entry_declared_names,
    } = facts;
    let mut needed = BTreeSet::<String>::new();
    // `body_facts_by_module` is precomputed once upstream (see
    // `lower_chunk`); both this auto-grow pass and the per-plan
    // reference resolver read the same `ModuleBodyFacts`, so the
    // body walk is paid exactly once per moved module.
    for facts in body_facts_by_module {
        for id in &facts.referenced_idents {
            if facts.provided_locals.contains(id) {
                continue;
            }
            if binding_assignment.contains_key(id) {
                continue;
            }
            if !declaration_by_name.contains_key(id) {
                continue;
            }
            if pre_existing_entry_exports.contains(id) {
                continue;
            }
            needed.insert(id.0.as_ref().to_string());
        }
    }
    // The ledger's `EntryPublicExports` taken-name set holds every
    // public name already committed to entry's export list — the
    // source-level set the caller seeded, plus each new grown public
    // name as it is minted. When a candidate's natural public name
    // collides, suffix-mint a fresh `<name>$<n>` instead of skipping:
    // skipping forces the peeled module's body reference to resolve as
    // an unexported residual binding and
    // `residual_entry_imports_for_moved_body` would bail with "moved
    // module references residual entry binding(s) not exported by
    // entry". The peeled module's importer side renames the import back
    // to the original local sym via
    // `EntryExport.{local_name, exported_name}`, so the mint is
    // invisible to the moved body.
    for name in needed {
        // Only grow exports for bindings the final entry body
        // actually declares. A chunk-declared binding whose
        // declaring statement was claimed into a non-entry
        // module (e.g. a block-hoisted `var` inside an
        // anonymously-claimed `try` statement) can't be
        // mediated through entry — growing `export { name }`
        // here would emit a SyntaxError-at-load entry module.
        // Skipping leaves the reference in
        // `missing_residual_exports`, which
        // `residual_entry_imports_for_moved_body` rejects
        // loudly instead of emitting broken JS.
        if !entry_declared_names.contains(&name) {
            continue;
        }
        let public_name = ledger.mint(RenameScope::EntryPublicExports, &name);
        ledger.submit(RenameIntent {
            scope: RenameScope::EntryPublicExports,
            from: top_level_id(&name, chunk_top_level_mark),
            to: public_name.as_str().into(),
            origin: RenameOrigin::ImportInduced {
                contributor: AUTO_GROWN_EXPORT_CONTRIBUTOR,
            },
        });
    }
}
