//! Which intrinsics a chunk itself modifies, from syntax.
//!
//! Tier 0 accepts some uses of intrinsics as pure (`new Map()`, `class X
//! extends Error`, `Object.prototype.toString`). Each depends on the global
//! binding still naming the intrinsic and on specific properties being
//! untouched. This census records, per global name, the properties the chunk
//! writes; a use the census cannot prove untouched stays impure.
//!
//! It sees this chunk only. Writes by other chunks are a per-program fact
//! the caller must supply.

use rustc_hash::{FxHashMap, FxHashSet};
use swc_common::{BytePos, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Writes {
    /// The global is replaced, escapes as a value, or is written under a key
    /// the census cannot name.
    pub any: bool,
    pub props: FxHashSet<String>,
}

#[derive(Default, Clone)]
pub struct Census {
    pub written: FxHashMap<String, Writes>,
    /// A store on the global object under an unknown key.
    all: bool,
}

impl Census {
    /// Adds what another chunk writes.
    pub fn merge(&mut self, other: Census) {
        self.all |= other.all;
        for (global, writes) in other.written {
            let mine = self.written.entry(global).or_default();
            mine.any |= writes.any;
            mine.props.extend(writes.props);
        }
    }

    /// Whether the chunk leaves the global binding and these properties of
    /// it (or of its `prototype`) untouched.
    pub fn untouched(&self, global: &str, props: &[&str]) -> bool {
        if self.all {
            return false;
        }
        match self.written.get(global) {
            None => true,
            Some(writes) => !writes.any && props.iter().all(|prop| !writes.props.contains(*prop)),
        }
    }
}

const GLOBAL_OBJECTS: [&str; 4] = ["globalThis", "window", "self", "global"];

/// Functions that modify their first argument.
const MUTATORS: [(&str, &str); 10] = [
    ("Object", "defineProperty"),
    ("Object", "defineProperties"),
    ("Object", "assign"),
    ("Object", "setPrototypeOf"),
    ("Object", "freeze"),
    ("Object", "seal"),
    ("Object", "preventExtensions"),
    ("Reflect", "defineProperty"),
    ("Reflect", "set"),
    ("Reflect", "setPrototypeOf"),
];

struct Visitor<'a> {
    unresolved: SyntaxContext,
    object_aliases: &'a FxHashMap<Id, String>,
    census: Census,
    mentions: FxHashMap<String, usize>,
    /// Mentions in a position that only reads the global: a callee, a member
    /// base, an `instanceof` or equality operand, a class heritage, a
    /// `typeof` operand.
    reads: FxHashMap<String, usize>,
    /// `G.prototype` expressions used as a member base or mutator target.
    consumed: FxHashSet<BytePos>,
}

fn strip(mut expr: &Expr) -> &Expr {
    while let Expr::Paren(paren) = expr {
        expr = &paren.expr;
    }
    expr
}

/// A key the census can name: an identifier, a string literal, or a
/// well-known symbol.
fn key(prop: &MemberProp, unresolved: SyntaxContext) -> Option<String> {
    match prop {
        MemberProp::Ident(name) => Some(name.sym.to_string()),
        MemberProp::PrivateName(_) => None,
        MemberProp::Computed(computed) => match strip(&computed.expr) {
            Expr::Lit(Lit::Str(value)) => Some(js_ast::str_value(value)),
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(name),
                ..
            }) if matches!(&**obj, Expr::Ident(symbol)
                if symbol.ctxt == unresolved && &*symbol.sym == "Symbol") =>
            {
                Some(format!("@@{}", name.sym))
            }
            _ => None,
        },
    }
}

impl Visitor<'_> {
    fn global<'e>(&self, expr: &'e Expr) -> Option<&'e Ident> {
        match strip(expr) {
            Expr::Ident(ident) if ident.ctxt == self.unresolved => Some(ident),
            _ => None,
        }
    }

    /// The global a member chain is rooted at, and its keys outward.
    fn chain(&self, expr: &Expr) -> Option<(String, Vec<Option<String>>)> {
        match strip(expr) {
            Expr::Ident(ident) if ident.ctxt == self.unresolved => {
                Some((ident.sym.to_string(), Vec::new()))
            }
            Expr::Member(member) => {
                let (root, mut keys) = self.chain(&member.obj)?;
                keys.push(key(&member.prop, self.unresolved));
                Some((root, keys))
            }
            _ => None,
        }
    }

    fn read(&mut self, ident: &Ident) {
        *self.reads.entry(ident.sym.to_string()).or_default() += 1;
    }

    /// Records a write to the object `target` names, under `prop`.
    fn write(&mut self, target: &Expr, prop: Option<String>) {
        let Some((mut root, mut keys)) = self.chain(target) else {
            return;
        };
        if GLOBAL_OBJECTS.contains(&root.as_str()) {
            // `globalThis.Map.prototype.set = …`, `window[k] = …`.
            match keys.first().cloned() {
                Some(Some(name)) => {
                    root = name;
                    keys.remove(0);
                }
                Some(None) => {
                    self.census.all = true;
                    return;
                }
                None => match prop {
                    // `globalThis.Map = …` replaces the binding.
                    Some(name) => {
                        self.census.written.entry(name).or_default().any = true;
                        return;
                    }
                    None => {
                        self.census.all = true;
                        return;
                    }
                },
            }
        }
        let writes = self.census.written.entry(root).or_default();
        let on_object_or_prototype = match keys.as_slice() {
            [] => true,
            [Some(name)] => name == "prototype",
            _ => false,
        };
        match prop {
            Some(prop) if on_object_or_prototype => {
                writes.props.insert(prop);
            }
            _ => writes.any = true,
        }
    }

    /// Marks `target` as the target of a write, so its `G.prototype` is not
    /// counted as an escaping value.
    fn consume(&mut self, target: &Expr) {
        if let Expr::Member(member) = strip(target) {
            self.consumed.insert(member.span.lo);
        }
    }

    fn mutator(&self, callee: &Expr) -> Option<&'static str> {
        let (object, method) = match strip(callee) {
            Expr::Member(MemberExpr {
                obj,
                prop: MemberProp::Ident(method),
                ..
            }) => (self.global(obj)?.sym.to_string(), method.sym.to_string()),
            Expr::Ident(alias) => (
                "Object".to_string(),
                self.object_aliases.get(&alias.to_id())?.clone(),
            ),
            _ => return None,
        };
        MUTATORS
            .iter()
            .find(|(owner, name)| *owner == object && *name == method)
            .map(|(_, name)| *name)
    }
}

impl Visit for Visitor<'_> {
    fn visit_ident(&mut self, ident: &Ident) {
        if ident.ctxt == self.unresolved {
            *self.mentions.entry(ident.sym.to_string()).or_default() += 1;
        }
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        if let Expr::Member(base) = strip(&member.obj) {
            self.consumed.insert(base.span.lo);
        }
        if let Some(root) = self.global(&member.obj) {
            self.read(root);
            let prototype =
                matches!(&member.prop, MemberProp::Ident(name) if &*name.sym == "prototype");
            if prototype && !self.consumed.contains(&member.span.lo) {
                // The prototype object itself is used as a value.
                let root = root.sym.to_string();
                self.census.written.entry(root).or_default().any = true;
            }
        }
        member.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, assign: &AssignExpr) {
        if let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assign.left {
            self.consume(&member.obj);
            self.write(&member.obj, key(&member.prop, self.unresolved));
        }
        if let AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) = &assign.left
            && binding.id.ctxt == self.unresolved
        {
            let name = binding.id.sym.to_string();
            self.census.written.entry(name).or_default().any = true;
        }
        assign.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, update: &UpdateExpr) {
        if let Expr::Member(member) = strip(&update.arg) {
            self.consume(&member.obj);
            self.write(&member.obj, key(&member.prop, self.unresolved));
        }
        update.visit_children_with(self);
    }

    fn visit_unary_expr(&mut self, unary: &UnaryExpr) {
        match unary.op {
            UnaryOp::Delete => {
                if let Expr::Member(member) = strip(&unary.arg) {
                    self.consume(&member.obj);
                    self.write(&member.obj, key(&member.prop, self.unresolved));
                }
            }
            UnaryOp::TypeOf => {
                if let Some(ident) = self.global(&unary.arg) {
                    self.read(ident);
                }
            }
            _ => {}
        }
        unary.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, call: &CallExpr) {
        if let Callee::Expr(callee) = &call.callee {
            if let Some(ident) = self.global(callee) {
                self.read(ident);
            }
            if let (Some(mutator), Some(target)) = (self.mutator(callee), call.args.first()) {
                self.consume(&target.expr);
                // A bare global as the target is read here, not leaked.
                if let Some(ident) = self.global(&target.expr) {
                    self.read(ident);
                }
                let prop = match (mutator, call.args.get(1)) {
                    ("defineProperty" | "set", Some(name)) => match strip(&name.expr) {
                        Expr::Lit(Lit::Str(value)) => Some(js_ast::str_value(value)),
                        _ => None,
                    },
                    _ => None,
                };
                self.write(&target.expr, prop);
            }
        }
        call.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, new: &NewExpr) {
        if let Some(ident) = self.global(&new.callee) {
            self.read(ident);
        }
        new.visit_children_with(self);
    }

    fn visit_bin_expr(&mut self, binary: &BinExpr) {
        if binary.op == BinaryOp::InstanceOf
            && let Some(ident) = self.global(&binary.right)
        {
            self.read(ident);
        }
        // An identity comparison keeps no reference.
        if matches!(
            binary.op,
            BinaryOp::EqEqEq | BinaryOp::NotEqEq | BinaryOp::EqEq | BinaryOp::NotEq
        ) {
            for operand in [&binary.left, &binary.right] {
                if let Some(ident) = self.global(operand) {
                    self.read(ident);
                }
            }
        }
        binary.visit_children_with(self);
    }

    fn visit_class(&mut self, class: &Class) {
        if let Some(heritage) = &class.super_class
            && let Some(ident) = self.global(heritage)
        {
            self.read(ident);
        }
        class.visit_children_with(self);
    }
}

pub fn census(
    module: &Module,
    unresolved: SyntaxContext,
    object_aliases: &FxHashMap<Id, String>,
) -> Census {
    let mut visitor = Visitor {
        unresolved,
        object_aliases,
        census: Census::default(),
        mentions: FxHashMap::default(),
        reads: FxHashMap::default(),
        consumed: FxHashSet::default(),
    };
    module.visit_with(&mut visitor);
    // A mention that is not a read lets the global flow somewhere the census
    // does not follow.
    for (name, mentions) in &visitor.mentions {
        if visitor.reads.get(name).copied().unwrap_or(0) != *mentions {
            visitor.census.written.entry(name.clone()).or_default().any = true;
        }
    }
    visitor.census
}
