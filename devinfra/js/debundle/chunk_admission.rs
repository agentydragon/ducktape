//! Input-chunk admission scan for the statically-checkable
//! assumptions of docs/design.md §"Conditions on the input chunk"
//! (A1 `eval`, A3 internal dynamic `import()`, A5 cheap
//! `import.meta` reflection shapes). Runs in Stage A
//! ([`crate::compute_stage_one_analysis`]) right next to the A2
//! top-level-await bail, before any quotient or lowering work.
//!
//! The scan is deliberately partial — it enforces the cheap,
//! low-false-positive sub-shapes of each assumption and leaves the
//! rest as documented input-shape contracts (see docs/design.md
//! §"Coverage gaps"). Per-check escape hatch:
//! `chunk_analysis_options.<chunk>.admission_overrides` in the spec
//! ([`spec::AdmissionOverrides`]) for chunks the author has audited.
//!
//! Notable residual gaps (documented, not checked):
//!
//! - **A1**: `eval` inside function bodies and other lazy positions.
//!   A1's wording only bans module-top `eval`, but a lazy direct
//!   `eval` can still read cross-module bindings dynamically when
//!   the enclosing function is called after the split. Aliased eval
//!   (`const e = eval; e(...)`) is also unchecked.
//! - **A3**: non-literal dynamic-import specifiers in lazy positions
//!   (`(n) => import(n)`), which could resolve to an internal module
//!   at runtime. Only `Lit::Str` specifiers count as literal — the
//!   same notion `prepare_js_chunks` / `rewrite_chunk_entry_specifiers`
//!   use; a zero-expr template literal is treated as non-literal.
//! - **A5**: `Function.prototype.toString` reads of chunk bindings,
//!   `Reflect` descriptor inspection on namespace objects, and any
//!   `import.meta` use inside lazy positions.
//!
//! **A4 (`with` blocks) is not checked here** because it never
//! reaches Stage A: module code is strict per ECMA-262, and the
//! production parser rejects `with` as a recoverable parse error
//! even with `no_early_errors: true`
//! (`js_ast::parse_module_from_source_file` fails on any recovered
//! error). The rejection is pinned by
//! `e2e/chunk_admission_test.rs::with_block_is_rejected_at_parse`.

use anyhow::{Result, bail};
use swc_ecma_ast::{
    ArrowExpr, CallExpr, Callee, Class, ClassMember, Expr, Function, GetterProp, Key, Lit,
    MemberExpr, MemberProp, MetaPropExpr, MetaPropKind, MethodProp, Module, PropName, SetterProp,
};
use swc_ecma_visit::{Visit, VisitWith};

use binding_targets::{callee_base_expr, strip_parens};
use spec::{AdmissionCheck, AdmissionOverrides};

use crate::facts::top_level_item_views;

/// Where a dynamic-import specifier resolves, from the caller chunk's
/// perspective. Produced by the artifact-aware resolver the lowering
/// layer passes into Stage A (Stage A itself is artifact-free).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DynamicImportTarget {
    /// Resolves to a file of the chunk being analyzed — a debundled
    /// internal module (A3 violation).
    SameChunk,
    /// Resolves to another chunk in the artifact (vendor / route
    /// chunk). Allowed: the realizability proof treats these as
    /// black-box leaves with their own evaluation (docs/design.md A3).
    OtherChunk,
    /// Does not resolve inside the artifact (external URL, bare
    /// package specifier, missing file). Allowed.
    External,
}

#[derive(Debug)]
struct AdmissionViolation {
    check: AdmissionCheck,
    /// Source-order statement ordinal in the post-comma-split view
    /// (`facts::top_level_item_views`), aligned with the A2 bail and
    /// the owner-graph reports.
    ordinal: usize,
    description: String,
}

/// Scan `module` for admission violations and `bail!` on any that the
/// spec does not override. Overridden violations print a one-line
/// notice; configured overrides that suppress nothing print a
/// redundant-override warning (mirroring the redundant-purity-hint
/// diagnostics).
pub fn enforce_chunk_admission(
    chunk_id: &str,
    module: &Module,
    overrides: AdmissionOverrides,
    resolve_dynamic_import: &dyn Fn(&str) -> DynamicImportTarget,
) -> Result<()> {
    let violations = scan_admission_violations(module, resolve_dynamic_import);
    let (suppressed, fatal): (Vec<_>, Vec<_>) = violations
        .into_iter()
        .partition(|violation| overrides.contains(violation.check));
    for violation in &suppressed {
        eprintln!(
            "notice: chunk {chunk_id}: admission check {check} overridden by spec \
             (chunk_analysis_options.{chunk_id}.admission_overrides): statement \
             #{ordinal}: {description}",
            check = violation.check,
            ordinal = violation.ordinal,
            description = violation.description,
        );
    }
    for check in overrides.iter() {
        if !suppressed.iter().any(|violation| violation.check == check) {
            eprintln!(
                "warning: chunk {chunk_id}: admission override `{check}` is redundant — \
                 the chunk has no {check} violation and the override is a no-op. \
                 Remove it from chunk_analysis_options.{chunk_id}.admission_overrides.",
            );
        }
    }
    if !fatal.is_empty() {
        let rendered = fatal
            .iter()
            .map(|violation| {
                format!(
                    "  statement #{ordinal}: [{check}] {description}",
                    ordinal = violation.ordinal,
                    check = violation.check,
                    description = violation.description,
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        bail!(
            "materialize_logical_modules: chunk {chunk_id} fails input-chunk admission \
             (docs/design.md §\"Conditions on the input chunk\"):\n{rendered}\n\
             The realizability theorem does not cover these shapes. Rework the chunk, or — \
             after auditing that the shape is benign here — opt out per check via \
             `chunk_analysis_options.{chunk_id}.admission_overrides` (e.g. `[{first}]`).",
            first = fatal[0].check,
        );
    }
    Ok(())
}

fn scan_admission_violations(
    module: &Module,
    resolve_dynamic_import: &dyn Fn(&str) -> DynamicImportTarget,
) -> Vec<AdmissionViolation> {
    let mut scan = AdmissionScan {
        resolve_dynamic_import,
        ordinal: 0,
        lazy_depth: 0,
        violations: Vec::new(),
    };
    for (ordinal, item) in top_level_item_views(&module.body).iter().enumerate() {
        scan.ordinal = ordinal;
        item.as_module_item().visit_with(&mut scan);
    }
    scan.violations
}

/// One-pass visitor over a single top-level statement. Tracks
/// `lazy_depth` across function/arrow/method/getter/setter bodies and
/// class instance-member initializers — the same lazy boundaries as
/// `facts::StatementFactsCollector` — so the eager-only checks (A1,
/// A3 non-literal, A5) fire only on code that runs during module
/// evaluation, while the any-depth check (A3 literal target) still
/// descends into lazy bodies.
struct AdmissionScan<'r> {
    resolve_dynamic_import: &'r dyn Fn(&str) -> DynamicImportTarget,
    ordinal: usize,
    lazy_depth: u32,
    violations: Vec<AdmissionViolation>,
}

impl AdmissionScan<'_> {
    fn record(&mut self, check: AdmissionCheck, description: String) {
        self.violations.push(AdmissionViolation {
            check,
            ordinal: self.ordinal,
            description,
        });
    }

    fn descend_lazy(&mut self, f: impl FnOnce(&mut Self)) {
        self.lazy_depth += 1;
        f(self);
        self.lazy_depth -= 1;
    }

    fn check_eval_callee(&mut self, callee: &Expr) {
        // `callee_base_expr` looks through (possibly nested,
        // paren-wrapped) comma sequences to the expression that
        // produces the callee value: `(0, eval)(...)` and
        // `((0, (0, eval)))(...)` both reach the trailing `eval`.
        // Module code is strict, and strict mode forbids `eval` as a
        // binding name — so an `eval` ident here is the global eval.
        if matches!(callee_base_expr(callee), Expr::Ident(ident) if ident.sym.as_ref() == "eval") {
            let shape = if matches!(strip_parens(callee), Expr::Seq(_)) {
                "indirect `(…, eval)(...)` call"
            } else {
                "direct `eval(...)` call"
            };
            self.record(
                AdmissionCheck::A1Eval,
                format!(
                    "{shape} at module top level — the static analyzer cannot see what \
                     eval reads, so the at-init read graph `I` would be incomplete"
                ),
            );
        }
    }

    fn check_dynamic_import(&mut self, node: &CallExpr) {
        let literal = node.args.first().and_then(|arg| {
            if arg.spread.is_some() {
                return None;
            }
            match &*arg.expr {
                Expr::Lit(Lit::Str(s)) => Some(s.value.to_string_lossy().to_string()),
                _ => None,
            }
        });
        match literal {
            Some(specifier) => {
                if (self.resolve_dynamic_import)(&specifier) == DynamicImportTarget::SameChunk {
                    self.record(
                        AdmissionCheck::A3DynamicImport,
                        format!(
                            "`import({specifier:?})` resolves into this chunk — dynamic \
                             import of a debundled internal module routes around the \
                             static import graph"
                        ),
                    );
                }
            }
            None if self.lazy_depth == 0 => {
                self.record(
                    AdmissionCheck::A3DynamicImport,
                    "dynamic `import(...)` with a non-literal specifier at module top \
                     level — the target cannot be proven external to the debundled \
                     module set"
                        .to_string(),
                );
            }
            None => {}
        }
    }
}

impl Visit for AdmissionScan<'_> {
    fn visit_call_expr(&mut self, node: &CallExpr) {
        match &node.callee {
            Callee::Import(_) => self.check_dynamic_import(node),
            Callee::Expr(expr) if self.lazy_depth == 0 => self.check_eval_callee(expr),
            _ => {}
        }
        node.visit_children_with(self);
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        if let Expr::MetaProp(meta) = strip_parens(&node.obj)
            && meta.kind == MetaPropKind::ImportMeta
        {
            if self.lazy_depth == 0 {
                match &node.prop {
                    // `import.meta.url` is the one whitelisted shape (A5).
                    MemberProp::Ident(prop) if prop.sym.as_ref() == "url" => {}
                    MemberProp::Ident(prop) => {
                        let prop = prop.sym.as_ref();
                        self.record(
                            AdmissionCheck::A5ImportMeta,
                            format!(
                                "`import.meta.{prop}` read at module top level — only \
                                 `import.meta.url` is covered by the realizability theorem"
                            ),
                        );
                    }
                    _ => {
                        self.record(
                            AdmissionCheck::A5ImportMeta,
                            "computed `import.meta[...]` access at module top level — only \
                             `import.meta.url` is covered by the realizability theorem"
                                .to_string(),
                        );
                    }
                }
            }
            // Never descend into the `import.meta` object itself — the
            // bare-`import.meta` visit below would double-flag it. A
            // computed prop expression may still contain checked
            // shapes, so visit just that.
            if let MemberProp::Computed(computed) = &node.prop {
                computed.expr.visit_with(self);
            }
            return;
        }
        node.visit_children_with(self);
    }

    fn visit_meta_prop_expr(&mut self, node: &MetaPropExpr) {
        // Reached only for `import.meta` outside the member shapes
        // intercepted above (e.g. passed as a value).
        if node.kind == MetaPropKind::ImportMeta && self.lazy_depth == 0 {
            self.record(
                AdmissionCheck::A5ImportMeta,
                "bare `import.meta` used as a value at module top level — namespace \
                 reflection beyond `import.meta.url`"
                    .to_string(),
            );
        }
    }

    // Lazy boundaries — mirror `facts::StatementFactsCollector`:
    // eager parts (keys, static members, decorators, extends) stay at
    // the current depth; bodies and instance initializers descend.
    fn visit_function(&mut self, node: &Function) {
        self.descend_lazy(|scan| node.visit_children_with(scan));
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.descend_lazy(|scan| node.visit_children_with(scan));
    }

    fn visit_method_prop(&mut self, node: &MethodProp) {
        node.key.visit_with(self);
        // Dispatches to `visit_function`, which descends.
        node.function.visit_with(self);
    }

    fn visit_getter_prop(&mut self, node: &GetterProp) {
        node.key.visit_with(self);
        self.descend_lazy(|scan| {
            if let Some(body) = &node.body {
                body.visit_with(scan);
            }
        });
    }

    fn visit_setter_prop(&mut self, node: &SetterProp) {
        node.key.visit_with(self);
        node.param.visit_with(self);
        self.descend_lazy(|scan| {
            if let Some(body) = &node.body {
                body.visit_with(scan);
            }
        });
    }

    fn visit_class(&mut self, node: &Class) {
        for decorator in &node.decorators {
            decorator.visit_with(self);
        }
        if let Some(super_class) = &node.super_class {
            super_class.visit_with(self);
        }
        for member in &node.body {
            member.visit_with(self);
        }
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        match member {
            ClassMember::Method(method) => {
                visit_computed_prop_name(self, &method.key);
                // Dispatches to `visit_function`, which descends.
                method.function.visit_with(self);
            }
            ClassMember::PrivateMethod(method) => {
                method.function.visit_with(self);
            }
            ClassMember::Constructor(ctor) => {
                self.descend_lazy(|scan| ctor.visit_children_with(scan));
            }
            ClassMember::ClassProp(prop) => {
                visit_computed_prop_name(self, &prop.key);
                if let Some(value) = &prop.value {
                    if prop.is_static {
                        value.visit_with(self);
                    } else {
                        self.descend_lazy(|scan| value.visit_with(scan));
                    }
                }
            }
            ClassMember::PrivateProp(prop) => {
                if let Some(value) = &prop.value {
                    if prop.is_static {
                        value.visit_with(self);
                    } else {
                        self.descend_lazy(|scan| value.visit_with(scan));
                    }
                }
            }
            ClassMember::StaticBlock(block) => {
                block.visit_with(self);
            }
            ClassMember::AutoAccessor(accessor) => {
                if let Key::Public(name) = &accessor.key {
                    visit_computed_prop_name(self, name);
                }
                if let Some(value) = &accessor.value {
                    if accessor.is_static {
                        value.visit_with(self);
                    } else {
                        self.descend_lazy(|scan| value.visit_with(scan));
                    }
                }
            }
            ClassMember::TsIndexSignature(_) | ClassMember::Empty(_) => {}
        }
    }
}

fn visit_computed_prop_name(scan: &mut AdmissionScan<'_>, name: &PropName) {
    if let PropName::Computed(computed) = name {
        computed.expr.visit_with(scan);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::{FileName, SourceMap, sync::Lrc};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    fn parse(source: &str) -> Module {
        let cm: Lrc<SourceMap> = Default::default();
        let fm = cm.new_source_file(
            FileName::Custom("test.js".into()).into(),
            source.to_string(),
        );
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            Default::default(),
            StringInput::from(&*fm),
            None,
        );
        Parser::new_from(lexer)
            .parse_module()
            .expect("parse module")
    }

    fn scan(source: &str) -> Vec<(AdmissionCheck, usize)> {
        scan_with_resolver(source, &|_| DynamicImportTarget::External)
    }

    fn scan_with_resolver(
        source: &str,
        resolve: &dyn Fn(&str) -> DynamicImportTarget,
    ) -> Vec<(AdmissionCheck, usize)> {
        scan_admission_violations(&parse(source), resolve)
            .into_iter()
            .map(|violation| (violation.check, violation.ordinal))
            .collect()
    }

    /// Paren-wrapped nested comma sequences still reach the trailing
    /// `eval`: the seq-aware callee unwrap is the point of this test.
    #[test]
    fn paren_wrapped_nested_seq_eval_is_flagged() {
        assert_eq!(
            scan("((0, (0, eval)))(\"x\");\n"),
            vec![(AdmissionCheck::A1Eval, 0)]
        );
    }

    /// `eval` inside a function body is a lazy position — A1 only
    /// bans module-top eval (the body's eval is a documented residual
    /// risk, not a violation).
    #[test]
    fn eval_in_function_body_is_not_flagged() {
        assert_eq!(scan("function f() { return eval(\"1\"); }\n"), vec![]);
    }

    /// A class static block runs at class evaluation (eager), so an
    /// eval there is module-top eval; an instance method body is lazy.
    #[test]
    fn class_static_block_is_eager_method_body_is_lazy() {
        assert_eq!(
            scan("class C { static { eval(\"1\"); } }\n"),
            vec![(AdmissionCheck::A1Eval, 0)]
        );
        assert_eq!(scan("class C { m() { eval(\"1\"); } }\n"), vec![]);
    }

    /// Literal dynamic imports resolve through the caller-supplied
    /// resolver: same-chunk targets are violations at any depth,
    /// other-chunk / external targets are allowed.
    #[test]
    fn literal_dynamic_import_targets() {
        let resolver = |specifier: &str| match specifier {
            "./self.js" => DynamicImportTarget::SameChunk,
            "./route.js" => DynamicImportTarget::OtherChunk,
            _ => DynamicImportTarget::External,
        };
        assert_eq!(
            scan_with_resolver("const f = () => import(\"./self.js\");\n", &resolver),
            vec![(AdmissionCheck::A3DynamicImport, 0)]
        );
        assert_eq!(
            scan_with_resolver(
                "import(\"./route.js\");\nimport(\"https://cdn/x.js\");\n",
                &resolver
            ),
            vec![]
        );
    }

    /// A zero-expr template literal is non-literal for admission (the
    /// rewrite/manifest passes only handle `Lit::Str` too): flagged at
    /// top level, allowed in lazy positions.
    #[test]
    fn non_literal_specifier_only_flagged_at_top_level() {
        assert_eq!(
            scan("import(`./a.js`);\n"),
            vec![(AdmissionCheck::A3DynamicImport, 0)]
        );
        assert_eq!(scan("const f = (n) => import(n);\n"), vec![]);
    }

    /// `import.meta.url` is whitelisted; other props, computed
    /// access, and bare value use are flagged at top level only.
    #[test]
    fn import_meta_shapes() {
        assert_eq!(scan("const u = import.meta.url;\n"), vec![]);
        assert_eq!(
            scan("const e = import.meta.env;\n"),
            vec![(AdmissionCheck::A5ImportMeta, 0)]
        );
        assert_eq!(
            scan("const k = import.meta[key];\n"),
            vec![(AdmissionCheck::A5ImportMeta, 0)]
        );
        assert_eq!(
            scan("const m = import.meta;\n"),
            vec![(AdmissionCheck::A5ImportMeta, 0)]
        );
        assert_eq!(scan("const f = () => import.meta.env;\n"), vec![]);
    }

    /// Ordinals use the post-comma-split top-level view, matching the
    /// A2 bail and the owner-graph reports.
    #[test]
    fn ordinals_align_with_comma_split_view() {
        assert_eq!(
            scan("const a = 1, b = 2;\neval(\"1\");\n"),
            vec![(AdmissionCheck::A1Eval, 2)]
        );
    }
}
