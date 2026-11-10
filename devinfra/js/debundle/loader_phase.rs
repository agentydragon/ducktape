//! Mandatory suspension of host-loader sites (SPEC.md § Loader-closed
//! derivation).
//!
//! A site is mandatorily suspended when its innermost function is an async,
//! non-generator function and every path from that function's entry to the
//! site completes an `await`. Code after such an `await` runs in a promise job,
//! never on the stack that invoked the function. The walk follows evaluation
//! order: an `await` operand is visited before the suspension it causes, a
//! short-circuit or conditional operand joins with the path that skips it, a
//! loop body may run zero times, and `catch`/`finally` start from the `try`
//! entry state. Abrupt completions leave the current path; `break` joins its
//! target's exit. A site on a path the walk considers dead stays unsuspended.

use std::collections::BTreeSet;

use swc_common::Span;
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

/// The source range identifying one census site.
pub(crate) type SiteKey = (u32, u32);

pub(crate) fn site_key(span: Span) -> SiteKey {
    (span.lo.0, span.hi.0)
}

/// The subset of `sites` that are mandatorily suspended.
pub(crate) fn mandatorily_suspended_sites(
    module: &Module,
    sites: &BTreeSet<SiteKey>,
) -> BTreeSet<SiteKey> {
    if sites.is_empty() {
        return BTreeSet::new();
    }
    let mut walk = SuspensionWalk {
        sites,
        suspended: BTreeSet::new(),
        flow: Flow::Live { suspended: false },
        async_body: false,
        targets: Vec::new(),
        pending_labels: Vec::new(),
    };
    module.visit_with(&mut walk);
    walk.suspended
}

/// Path state at a program point: `Dead` when no path reaches it, otherwise
/// whether every reaching path has completed an `await`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Flow {
    Dead,
    Live { suspended: bool },
}

impl Flow {
    fn join(self, other: Self) -> Self {
        match (self, other) {
            (Self::Dead, flow) | (flow, Self::Dead) => flow,
            (Self::Live { suspended: a }, Self::Live { suspended: b }) => {
                Self::Live { suspended: a && b }
            }
        }
    }
}

/// A `break` target: loops and `switch` accept an unlabeled `break`; any
/// statement accepts a `break` naming one of its labels.
struct BreakTarget {
    labels: Vec<String>,
    unlabeled: bool,
    exit: Flow,
}

struct SuspensionWalk<'a> {
    sites: &'a BTreeSet<SiteKey>,
    suspended: BTreeSet<SiteKey>,
    flow: Flow,
    /// The innermost function is async and not a generator.
    async_body: bool,
    targets: Vec<BreakTarget>,
    /// Labels of a labeled statement, attached to the loop or `switch` it
    /// labels.
    pending_labels: Vec<String>,
}

impl SuspensionWalk<'_> {
    fn check(&mut self, span: Span) {
        let key = site_key(span);
        if self.async_body
            && self.flow == (Flow::Live { suspended: true })
            && self.sites.contains(&key)
        {
            self.suspended.insert(key);
        }
    }

    /// Walk a nested function body from a fresh, unsuspended entry.
    fn in_function(&mut self, is_async: bool, walk: impl FnOnce(&mut Self)) {
        let flow = std::mem::replace(&mut self.flow, Flow::Live { suspended: false });
        let async_body = std::mem::replace(&mut self.async_body, is_async);
        let targets = std::mem::take(&mut self.targets);
        let labels = std::mem::take(&mut self.pending_labels);
        walk(self);
        self.flow = flow;
        self.async_body = async_body;
        self.targets = targets;
        self.pending_labels = labels;
    }

    /// Walk a loop or `switch` body as a `break` target, returning the joined
    /// exit of its `break`s.
    fn breakable(&mut self, walk: impl FnOnce(&mut Self)) -> Flow {
        let labels = std::mem::take(&mut self.pending_labels);
        self.targets.push(BreakTarget {
            labels,
            unlabeled: true,
            exit: Flow::Dead,
        });
        walk(self);
        self.targets.pop().expect("pushed target").exit
    }

    fn suspend(&mut self) {
        if let Flow::Live { .. } = self.flow {
            self.flow = Flow::Live { suspended: true };
        }
    }
}

impl Visit for SuspensionWalk<'_> {
    fn visit_function(&mut self, node: &Function) {
        self.in_function(node.is_async && !node.is_generator, |walk| {
            node.visit_children_with(walk)
        });
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.in_function(node.is_async && !node.is_generator, |walk| {
            node.visit_children_with(walk)
        });
    }

    fn visit_constructor(&mut self, node: &Constructor) {
        node.key.visit_with(self);
        self.in_function(false, |walk| {
            node.params.visit_with(walk);
            node.body.visit_with(walk);
        });
    }

    fn visit_class_prop(&mut self, node: &ClassProp) {
        node.key.visit_with(self);
        self.in_function(false, |walk| node.value.visit_with(walk));
    }

    fn visit_private_prop(&mut self, node: &PrivateProp) {
        self.in_function(false, |walk| node.value.visit_with(walk));
    }

    fn visit_static_block(&mut self, node: &StaticBlock) {
        self.in_function(false, |walk| node.body.visit_with(walk));
    }

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        node.arg.visit_with(self);
        self.suspend();
    }

    fn visit_bin_expr(&mut self, node: &BinExpr) {
        node.left.visit_with(self);
        if matches!(
            node.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ) {
            let skipped = self.flow;
            node.right.visit_with(self);
            self.flow = self.flow.join(skipped);
        } else {
            node.right.visit_with(self);
        }
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        node.left.visit_with(self);
        if matches!(
            node.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            let skipped = self.flow;
            node.right.visit_with(self);
            self.flow = self.flow.join(skipped);
        } else {
            node.right.visit_with(self);
        }
    }

    fn visit_cond_expr(&mut self, node: &CondExpr) {
        node.test.visit_with(self);
        let entry = self.flow;
        node.cons.visit_with(self);
        let cons = std::mem::replace(&mut self.flow, entry);
        node.alt.visit_with(self);
        self.flow = self.flow.join(cons);
    }

    fn visit_opt_chain_expr(&mut self, node: &OptChainExpr) {
        // Any link may short-circuit the rest of the chain.
        let entry = self.flow;
        node.visit_children_with(self);
        self.flow = self.flow.join(entry);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        node.visit_children_with(self);
        self.check(node.span);
    }

    fn visit_member_expr(&mut self, node: &MemberExpr) {
        node.visit_children_with(self);
        self.check(node.span);
    }

    fn visit_ident(&mut self, node: &Ident) {
        self.check(node.span);
    }

    fn visit_meta_prop_expr(&mut self, node: &MetaPropExpr) {
        self.check(node.span);
    }

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        node.arg.visit_with(self);
        self.flow = Flow::Dead;
    }

    fn visit_throw_stmt(&mut self, node: &ThrowStmt) {
        node.arg.visit_with(self);
        self.flow = Flow::Dead;
    }

    /// A later iteration starts no less suspended than the first, which the
    /// loop body already covers.
    fn visit_continue_stmt(&mut self, _node: &ContinueStmt) {
        self.flow = Flow::Dead;
    }

    fn visit_break_stmt(&mut self, node: &BreakStmt) {
        let flow = self.flow;
        let target = self
            .targets
            .iter_mut()
            .rev()
            .find(|target| match &node.label {
                Some(label) => target.labels.iter().any(|name| *name == *label.sym),
                None => target.unlabeled,
            });
        if let Some(target) = target {
            target.exit = target.exit.join(flow);
        }
        self.flow = Flow::Dead;
    }

    fn visit_labeled_stmt(&mut self, node: &LabeledStmt) {
        self.pending_labels.push(node.label.sym.to_string());
        let labels_loop_or_switch = matches!(
            &*node.body,
            Stmt::While(_)
                | Stmt::DoWhile(_)
                | Stmt::For(_)
                | Stmt::ForIn(_)
                | Stmt::ForOf(_)
                | Stmt::Switch(_)
                | Stmt::Labeled(_)
        );
        if labels_loop_or_switch {
            node.body.visit_with(self);
            return;
        }
        let labels = std::mem::take(&mut self.pending_labels);
        self.targets.push(BreakTarget {
            labels,
            unlabeled: false,
            exit: Flow::Dead,
        });
        node.body.visit_with(self);
        let exit = self.targets.pop().expect("pushed target").exit;
        self.flow = self.flow.join(exit);
    }

    fn visit_if_stmt(&mut self, node: &IfStmt) {
        node.test.visit_with(self);
        let entry = self.flow;
        node.cons.visit_with(self);
        let cons = std::mem::replace(&mut self.flow, entry);
        node.alt.visit_with(self);
        self.flow = self.flow.join(cons);
    }

    fn visit_while_stmt(&mut self, node: &WhileStmt) {
        node.test.visit_with(self);
        let skipped = self.flow;
        let exit = self.breakable(|walk| node.body.visit_with(walk));
        self.flow = skipped.join(exit);
    }

    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        let exit = self.breakable(|walk| {
            node.body.visit_with(walk);
            node.test.visit_with(walk);
        });
        self.flow = self.flow.join(exit);
    }

    fn visit_for_stmt(&mut self, node: &ForStmt) {
        node.init.visit_with(self);
        node.test.visit_with(self);
        let skipped = self.flow;
        let exit = self.breakable(|walk| {
            node.body.visit_with(walk);
            node.update.visit_with(walk);
        });
        self.flow = skipped.join(exit);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        node.right.visit_with(self);
        let skipped = self.flow;
        let exit = self.breakable(|walk| {
            node.left.visit_with(walk);
            node.body.visit_with(walk);
        });
        self.flow = skipped.join(exit);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        node.right.visit_with(self);
        // `for await` awaits each step result, including the one that ends
        // the loop, before the body or the exit.
        if node.is_await {
            self.suspend();
        }
        let skipped = self.flow;
        let exit = self.breakable(|walk| {
            node.left.visit_with(walk);
            node.body.visit_with(walk);
        });
        self.flow = skipped.join(exit);
    }

    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        node.discriminant.visit_with(self);
        let entry = self.flow;
        let exit = self.breakable(|walk| {
            // A case body is entered from its test or by fallthrough from an
            // earlier case, both no less suspended than the discriminant.
            for case in &node.cases {
                walk.flow = entry;
                case.test.visit_with(walk);
                case.cons.visit_with(walk);
            }
        });
        self.flow = entry.join(self.flow).join(exit);
    }

    fn visit_try_stmt(&mut self, node: &TryStmt) {
        let entry = self.flow;
        node.block.visit_with(self);
        if let Some(handler) = &node.handler {
            // Any point of the `try` block, including its entry, may throw.
            let block = std::mem::replace(&mut self.flow, entry);
            handler.visit_with(self);
            self.flow = self.flow.join(block);
        }
        if let Some(finalizer) = &node.finalizer {
            let completed = std::mem::replace(&mut self.flow, entry);
            finalizer.visit_with(self);
            if completed == Flow::Dead {
                self.flow = Flow::Dead;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::GLOBALS;

    /// Each `import.meta.require` call in `source` and whether it is
    /// mandatorily suspended, in source order.
    fn suspended(source: &str) -> Vec<bool> {
        GLOBALS.set(&Default::default(), || {
            let parsed = js_ast::parse_js_module("fixture.js", source).expect("parse");
            let mut calls = LoaderCalls(Vec::new());
            parsed.module.visit_with(&mut calls);
            let sites: BTreeSet<_> = calls.0.iter().copied().collect();
            let result = mandatorily_suspended_sites(&parsed.module, &sites);
            calls.0.iter().map(|site| result.contains(site)).collect()
        })
    }

    struct LoaderCalls(Vec<SiteKey>);

    impl Visit for LoaderCalls {
        fn visit_call_expr(&mut self, node: &CallExpr) {
            node.visit_children_with(self);
            if let Callee::Expr(callee) = &node.callee
                && let Expr::Member(member) = &**callee
                && matches!(&*member.obj, Expr::MetaProp(_))
            {
                self.0.push(site_key(node.span));
            }
        }
    }

    #[test]
    fn suspension_needs_an_await_on_every_path_in_an_async_function() {
        for (source, expected) in [
            (
                "async function f() { await x; import.meta.require('a'); }",
                vec![true],
            ),
            (
                "async function f() { import.meta.require('a'); await x; }",
                vec![false],
            ),
            ("function f() { import.meta.require('a'); }", vec![false]),
            (
                "async function* f() { await x; import.meta.require('a'); }",
                vec![false],
            ),
            (
                "async function f() { await x; return () => import.meta.require('a'); }",
                vec![false],
            ),
            (
                "const f = async () => { await x; return import.meta.require('a'); };",
                vec![true],
            ),
            // The operand runs before the suspension it causes.
            (
                "async function f() { await import.meta.require('a'); }",
                vec![false],
            ),
            (
                "async function f() { if (c) await x; import.meta.require('a'); }",
                vec![false],
            ),
            (
                "async function f() { if (c) await x; else await y; import.meta.require('a'); }",
                vec![true],
            ),
            (
                "async function f() { c && await x; import.meta.require('a'); }",
                vec![false],
            ),
            (
                "async function f() { c ? await x : await y; import.meta.require('a'); }",
                vec![true],
            ),
            (
                "async function f() { while (c) { await x; } import.meta.require('a'); }",
                vec![false],
            ),
            (
                "async function f() { do { await x; } while (c); import.meta.require('a'); }",
                vec![true],
            ),
            (
                "async function f() { for (;;) { if (c) break; await x; } import.meta.require('a'); }",
                vec![false],
            ),
            (
                "async function f() { for await (const v of s) {} import.meta.require('a'); }",
                vec![true],
            ),
            (
                "async function f() { try { await x; } catch { import.meta.require('a'); } }",
                vec![false],
            ),
            (
                "async function f() { try { await x; } finally { import.meta.require('a'); } }",
                vec![false],
            ),
            (
                "async function f() { try { await x; } catch { await y; } import.meta.require('a'); }",
                vec![true],
            ),
            (
                "async function f() { try { await x; } catch {} import.meta.require('a'); }",
                vec![false],
            ),
            (
                "async function f() { a: { if (c) break a; await x; } import.meta.require('a'); }",
                vec![false],
            ),
            (
                "async function f() { switch (v) { case 1: await x; break; default: await y; } import.meta.require('a'); }",
                vec![false],
            ),
            (
                "async function f() { await x; import.meta.require('a'); import.meta.require('b'); }",
                vec![true, true],
            ),
            (
                "async function f() { await x; const o = { get g() { return import.meta.require('a'); } }; }",
                vec![false],
            ),
        ] {
            assert_eq!(suspended(source), expected, "{source}");
        }
    }
}
