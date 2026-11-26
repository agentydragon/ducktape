//! The AST fact store (EDB) the selector solver runs over: a faithful,
//! **fail-closed** projection of a parsed chunk into AST facts.
//!
//! Fail-closed by construction: the walk's only catch-all is a loud
//! [`Unsupported`] error, so a node type it has not modeled **crashes** rather
//! than projecting a silently-incomplete fact set — which is what would let a
//! lowered selector query under-constrain and match the wrong source_unit. There is
//! deliberately no `swc_ecma_visit::Visit` here: its default no-op methods make
//! a forgotten node type a silent skip, the exact failure the goal forbids.
//!
//! The matcher (`source_match`) is the fidelity source of truth for which
//! children matter and in what order; this extractor mirrors those structural
//! decisions, and the corpus differential is what proves the mirror is faithful.
//! Constructs it does not model (e.g. TS-only nodes) raise [`Unsupported`].

mod extractor;
#[cfg(test)]
mod tests;

use extractor::Extractor;
use std::collections::{BTreeMap, HashMap};

use binding_targets::binding_names;
use js_ast::source_unit_ordinal_for_statement;
use source_identity::{SourceUnitOrdinal, StatementId};
use swc_ecma_ast::{
    AssignTarget, Decl, Expr, Module, ModuleDecl, ModuleItem, SimpleAssignTarget, Stmt,
    VarDeclarator,
};

pub type NodeId = u32;

/// The syntactic kind tag of a projected node — a closed enum over every
/// construct the extractor models. The matcher joins needle and subject node
/// kinds for structural equality (`source_match`), so a node type the extractor
/// does not model never reaches here: unmodeled constructs raise [`Unsupported`]
/// up front (see the module docstring), so this enum is exhaustive by
/// construction rather than carrying a catch-all.
///
/// `as_tag` exposes each variant's name for the string-keyed views (`Index`,
/// root-kind prefilters) the downstream resolver and near-miss diagnostics read.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, strum::IntoStaticStr)]
pub enum NodeKind {
    Array,
    ArrayPat,
    Arrow,
    Assign,
    AssignPat,
    AssignProp,
    AsyncArrow,
    AsyncFunction,
    AsyncGeneratorFunction,
    Await,
    BigIntLit,
    Bin,
    BindingIdent,
    Block,
    BoolLit,
    Break,
    Call,
    Catch,
    Class,
    ClassDecl,
    ClassExpr,
    ClassMemberEmpty,
    ClassProp,
    ComputedKey,
    Cond,
    Constructor,
    Continue,
    Debugger,
    DoWhile,
    Elision,
    Empty,
    ExportAll,
    ExportDecl,
    ExportDefault,
    ExportDefaultDecl,
    ExportNamed,
    ExprStmt,
    FnDecl,
    FnExpr,
    For,
    ForIn,
    ForOf,
    Function,
    GeneratorFunction,
    Getter,
    Ident,
    If,
    Import,
    ImportCallee,
    ImportSpecifier,
    KeyValue,
    Labeled,
    Member,
    MetaPropImportMeta,
    MetaPropNewTarget,
    Method,
    New,
    NullLit,
    NumLit,
    Object,
    ObjectMethod,
    ObjectPat,
    OptCall,
    OptChain,
    PatAssign,
    PatKeyValue,
    PropName,
    RegexLit,
    RestPat,
    Return,
    Seq,
    Setter,
    Shorthand,
    Spread,
    StaticBlock,
    StrLit,
    Super,
    SuperProp,
    Switch,
    SwitchCase,
    TaggedTpl,
    This,
    Throw,
    Tpl,
    TplQuasi,
    Try,
    Unary,
    UpdatePostfix,
    UpdatePrefix,
    VarDecl,
    VarDeclarator,
    While,
    Yield,
}

impl NodeKind {
    /// The variant's name. Feeds the string-keyed [`Index`] kind vector and the
    /// root-kind prefilters consumed by `source_match` (the resolver and
    /// near-miss walk).
    pub fn as_tag(self) -> &'static str {
        self.into()
    }
}

/// A faithful relational projection of one chunk's top-level statements. Each
/// vector is an EDB relation the lowered selector queries join over.
#[derive(Debug, Default)]
pub struct ChunkFacts {
    /// node -> syntactic kind tag.
    pub node_kind: Vec<(NodeId, NodeKind)>,
    /// parent -> (source-order ordinal, child). The ordinal is what the
    /// run-hole / adjacency encoding compares (`i < j`).
    pub child: Vec<(NodeId, u32, NodeId)>,
    /// string-literal value, unescaped via `js_ast::str_value`.
    pub str_lit: Vec<(NodeId, String)>,
    /// numeric-literal token, rendered faithfully (source `raw` when present).
    pub num_lit: Vec<(NodeId, String)>,
    pub bool_lit: Vec<(NodeId, bool)>,
    /// Identifier spelling. Exact identifier comparisons are retained as an
    /// internal lowering / solver constraint even though public `source_match`
    /// authoring uses alpha-all matching.
    pub ident_name: Vec<(NodeId, String)>,
    /// member / property / method name (non-computed).
    pub prop_name: Vec<(NodeId, String)>,
    /// distinguishing keyword/operator token: the operator for `Bin` / `Unary`
    /// / `Update` / `Assign`, and the declaration keyword (`var`/`let`/`const`)
    /// for `VarDecl`, plus class-member static/accessor modifiers. All are exact
    /// labels the matcher compares, including in alpha mode.
    pub operator: Vec<(NodeId, String)>,
    /// regex literal -> (pattern, flags), both T-invariant labels.
    pub regex: Vec<(NodeId, String, String)>,
    /// class node -> its super-class expression node (the syntactic `extends`).
    pub super_class: Vec<(NodeId, NodeId)>,
    /// top-level statement node -> its source_unit statement ordinal (dependency graph join).
    pub top_level: Vec<(NodeId, usize)>,
}

/// A construct the extractor has not modeled yet. Loud by design — never a
/// silent gap. `context` names where the gap is, so coverage can grow.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unsupported {
    pub context: &'static str,
}

impl std::fmt::Display for Unsupported {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.context)
    }
}

impl std::error::Error for Unsupported {}

fn unsupported<T>(context: &'static str) -> Result<T, Unsupported> {
    Err(Unsupported { context })
}

/// Name the unmodeled `Expr` variant so the coverage histogram ranks what to
/// implement next. Diagnostic only — fail-closed correctness is the error
/// itself, not the label precision; rare variants fall through to `expr:other`.
fn expr_variant_name(expr: &Expr) -> &'static str {
    match expr {
        Expr::Object(_) => "expr:object",
        Expr::Assign(_) => "expr:assign",
        Expr::Arrow(_) => "expr:arrow",
        Expr::Fn(_) => "expr:fn",
        Expr::Tpl(_) => "expr:tpl",
        Expr::TaggedTpl(_) => "expr:tagged_tpl",
        Expr::Await(_) => "expr:await",
        Expr::Update(_) => "expr:update",
        Expr::Yield(_) => "expr:yield",
        Expr::OptChain(_) => "expr:opt_chain",
        Expr::This(_) => "expr:this",
        Expr::MetaProp(_) => "expr:meta_prop",
        Expr::Class(_) => "expr:class",
        Expr::SuperProp(_) => "expr:super_prop",
        _ => "expr:other",
    }
}

/// Project a parsed chunk's top-level statements into AST facts, or fail loudly
/// at the first construct not yet modeled. Takes a borrowed item slice so a
/// caller projecting one statement at a time (the resolver's per-needle facts)
/// need not clone the item into a one-item [`Module`] first.
pub fn extract_facts_items(items: &[ModuleItem]) -> Result<ChunkFacts, Unsupported> {
    let mut extractor = Extractor::default();
    for (body_idx, item) in items.iter().enumerate() {
        let ordinal = source_unit_ordinal_for_statement(items, StatementId(body_idx)).0;
        extractor.module_item(item, ordinal)?;
    }
    Ok(extractor.facts)
}

/// Project one declarator as its own top-level variable declaration, retaining
/// the declaration kind and optional export wrapper. Borrow the selected AST
/// instead of cloning its initializer and all sibling declarators.
pub fn extract_single_declarator_facts(
    item: &ModuleItem,
    declarator: &VarDeclarator,
) -> Result<ChunkFacts, Unsupported> {
    let var = js_ast::item_var_decl(item).ok_or(Unsupported {
        context: "single declarator requires a variable declaration",
    })?;
    let mut extractor = Extractor::default();
    let export = matches!(item, ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(_)))
        .then(|| extractor.node(NodeKind::ExportDecl));
    let declaration = extractor.var_declarators(var, std::slice::from_ref(declarator))?;
    let root = if let Some(export) = export {
        extractor.facts.child.push((export, 0, declaration));
        export
    } else {
        declaration
    };
    extractor.facts.top_level.push((root, 0));
    Ok(extractor.facts)
}

/// Which AST relations the selector program needs. A module-member use is a
/// member read whose base joins to the chunk's import table in the resolver.
#[derive(Debug, Default)]
pub struct RelationalFactNeeds {
    pub member_reads: bool,
    pub call_arguments: bool,
    pub decorate_calls: bool,
    pub intrinsic_aliases: bool,
}

#[derive(Debug, Default)]
pub struct RelationalFacts {
    pub member_reads: Vec<(SourceUnitOrdinal, MemberReadFact)>,
    pub call_arguments: Vec<CallArgumentFact>,
    pub decorate_calls: Vec<DecorateCallFact>,
    pub intrinsic_aliases: Vec<IntrinsicAliasFact>,
}

/// A non-computed `obj.member` access. Only a bare identifier names the object.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemberReadFact {
    pub object: Option<String>,
    pub member: String,
}

/// A bare identifier passed to a non-computed member call, keyed by the
/// argument binding so the resolver can join it to its declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallArgumentFact {
    pub argument: String,
    pub callee_member: String,
    pub callee_object: Option<String>,
    pub arg_index: usize,
}

/// A structural match for an esbuild/TypeScript decorator application. The
/// callee names the target binding and the class and optional member anchor it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecorateCallFact {
    pub callee: String,
    pub class_anchor: String,
    pub member: Option<String>,
}

/// A bare binding initialized from a non-computed member of the unshadowed
/// global `Object`. The resolver joins it to the helper that references it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IntrinsicAliasFact {
    pub binding: String,
    pub property: String,
}

/// Project each statement once for the requested relations. An unsupported
/// subtree discards that statement's entire projection. Rows retain statement
/// and node order, with call arguments in positional order.
pub fn extract_relational_facts(
    module: &Module,
    mut needs: RelationalFactNeeds,
) -> RelationalFacts {
    if needs.intrinsic_aliases && module_top_level_binds_object(module) {
        needs.intrinsic_aliases = false;
    }
    let mut rows = RelationalFacts::default();
    if !(needs.member_reads
        || needs.call_arguments
        || needs.decorate_calls
        || needs.intrinsic_aliases)
    {
        return rows;
    }

    let mut ordinal = SourceUnitOrdinal(0);
    for item in &module.body {
        let statement_ordinal = ordinal;
        if needs.member_reads {
            ordinal.0 += js_ast::post_split_top_level_count(item);
        }
        let mut extractor = Extractor::default();
        if extractor.module_item(item, statement_ordinal.0).is_err() {
            continue;
        }
        let facts = &extractor.facts;
        let index = StatementFactIndex::new(facts, needs.decorate_calls);
        for &(node, kind) in &facts.node_kind {
            match kind {
                NodeKind::Member if needs.member_reads => {
                    if let Some((object, member)) = index.member(node) {
                        rows.member_reads.push((
                            statement_ordinal,
                            MemberReadFact {
                                object: object.map(str::to_owned),
                                member: member.to_owned(),
                            },
                        ));
                    }
                }
                NodeKind::Call | NodeKind::New | NodeKind::OptCall => {
                    if needs.call_arguments {
                        index.call_arguments(node, &mut rows.call_arguments);
                    }
                    if needs.decorate_calls && kind == NodeKind::Call {
                        rows.decorate_calls.extend(index.decorate_call(node));
                    }
                }
                NodeKind::VarDeclarator if needs.intrinsic_aliases => {
                    rows.intrinsic_aliases.extend(index.intrinsic_alias(node));
                }
                _ => {}
            }
        }
    }
    rows
}

struct StatementFactIndex<'a> {
    facts: &'a ChunkFacts,
    children: HashMap<(NodeId, u32), NodeId>,
    ident_names: HashMap<NodeId, &'a str>,
    prop_names: HashMap<NodeId, &'a str>,
    strings: HashMap<NodeId, &'a str>,
}

impl<'a> StatementFactIndex<'a> {
    fn new(facts: &'a ChunkFacts, decorate_calls: bool) -> Self {
        Self {
            facts,
            children: facts.child.iter().map(|&(p, i, c)| ((p, i), c)).collect(),
            ident_names: facts
                .ident_name
                .iter()
                .map(|(id, s)| (*id, s.as_str()))
                .collect(),
            prop_names: facts
                .prop_name
                .iter()
                .map(|(id, s)| (*id, s.as_str()))
                .collect(),
            strings: if decorate_calls {
                facts
                    .str_lit
                    .iter()
                    .map(|(id, s)| (*id, s.as_str()))
                    .collect()
            } else {
                HashMap::new()
            },
        }
    }

    fn child(&self, node: NodeId, ordinal: u32) -> Option<NodeId> {
        self.children.get(&(node, ordinal)).copied()
    }

    fn kind(&self, node: NodeId) -> NodeKind {
        // Extractor::node assigns IDs in node_kind insertion order.
        self.facts.node_kind[node as usize].1
    }

    fn ident(&self, node: NodeId, kind: NodeKind) -> Option<&'a str> {
        self.ident_names
            .get(&node)
            .copied()
            .filter(|_| self.kind(node) == kind)
    }

    fn member(&self, node: NodeId) -> Option<(Option<&'a str>, &'a str)> {
        if self.kind(node) != NodeKind::Member {
            return None;
        }
        let property = self.prop_names.get(&self.child(node, 1)?)?;
        let object = self
            .child(node, 0)
            .and_then(|node| self.ident(node, NodeKind::Ident));
        Some((object, property))
    }

    fn call_arguments(&self, node: NodeId, rows: &mut Vec<CallArgumentFact>) {
        let Some((object, member)) = self.child(node, 0).and_then(|callee| self.member(callee))
        else {
            return;
        };
        for arg_index in 0.. {
            let Some(arg) = self.child(node, arg_index + 1) else {
                break;
            };
            if let Some(argument) = self.ident(arg, NodeKind::Ident) {
                rows.push(CallArgumentFact {
                    argument: argument.to_owned(),
                    callee_member: member.to_owned(),
                    callee_object: object.map(str::to_owned),
                    arg_index: arg_index as usize,
                });
            }
        }
    }

    // The first argument must be an array literal of decorators. The class form
    // is H([...], C); the member form is H([...], C.prototype, "member"[, flags]).
    fn decorate_call(&self, node: NodeId) -> Option<DecorateCallFact> {
        let callee = self.ident(self.child(node, 0)?, NodeKind::Ident)?;
        if self.kind(self.child(node, 1)?) != NodeKind::Array || self.child(node, 5).is_some() {
            return None;
        }
        let target = self.child(node, 2)?;
        let class_anchor = match self.kind(target) {
            NodeKind::Ident => self.ident(target, NodeKind::Ident)?,
            NodeKind::Member => match self.member(target)? {
                (Some(class), "prototype") => class,
                _ => return None,
            },
            _ => return None,
        };
        let flags = self.child(node, 4);
        let member = match self.child(node, 3) {
            None if flags.is_none() => None,
            Some(arg)
                if flags.is_none_or(|flag| {
                    matches!(self.kind(flag), NodeKind::NumLit | NodeKind::BigIntLit)
                }) =>
            {
                Some(self.strings.get(&arg)?.to_string())
            }
            _ => return None,
        };
        Some(DecorateCallFact {
            callee: callee.to_owned(),
            class_anchor: class_anchor.to_owned(),
            member,
        })
    }

    fn intrinsic_alias(&self, node: NodeId) -> Option<IntrinsicAliasFact> {
        let binding = self.ident(self.child(node, 0)?, NodeKind::BindingIdent)?;
        let (object, property) = self.member(self.child(node, 1)?)?;
        if object != Some("Object") {
            return None;
        }
        Some(IntrinsicAliasFact {
            binding: binding.to_owned(),
            property: property.to_owned(),
        })
    }
}

/// `true` when any top-level declaration (incl. block-hoisted `var`s) or import
/// specifier of `module` binds the name `Object`, shadowing the global intrinsic.
/// The structural sibling of `analysis::facts::unshadowed_global_object_aliases`'s
/// shadow scan, specialized to the single name `Object`; kept here (not borrowed
/// from the `analysis` crate) so `chunk_facts` stays dependency-light.
fn module_top_level_binds_object(module: &Module) -> bool {
    module.body.iter().any(|item| match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => decl_binds_object(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => decl_binds_object(&export.decl),
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
            .specifiers
            .iter()
            .any(|spec| spec.local().sym == "Object"),
        // A bare `Object = ...` assignment (reassignment of the global) shadows the
        // intrinsic identity just as a declaration does; treat it as binding.
        ModuleItem::Stmt(stmt) => stmt_reassigns_object(stmt),
        _ => false,
    })
}

/// `true` when a declaration binds the name `Object` at the top level. A `var`
/// hoists out of blocks, so its name may appear nested; a `let`/`const`/`function`/
/// `class` binds directly.
fn decl_binds_object(decl: &Decl) -> bool {
    match decl {
        Decl::Class(class) => class.ident.sym.as_ref() == "Object",
        Decl::Fn(func) => func.ident.sym.as_ref() == "Object",
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(|declarator| binding_names(&declarator.name))
            .any(|(sym, _)| sym.as_ref() == "Object"),
        _ => false,
    }
}

/// `true` when a top-level statement reassigns the bare global `Object`
/// (`Object = ...`). A reassignment defeats the intrinsic-identity guard exactly
/// like a shadowing declaration, so the alias must fail closed.
fn stmt_reassigns_object(stmt: &Stmt) -> bool {
    let Stmt::Expr(expr_stmt) = stmt else {
        return false;
    };
    let Expr::Assign(assign) = expr_stmt.expr.as_ref() else {
        return false;
    };
    matches!(
        &assign.left,
        AssignTarget::Simple(SimpleAssignTarget::Ident(ident)) if ident.id.sym.as_ref() == "Object"
    )
}

/// Per-top-level-statement coverage of the extractor over a chunk: how many
/// statements fully extract vs. hit a fail-closed [`Unsupported`]. Run it over a
/// real chunk and grow the walk to clear the most frequent blocker first.
#[derive(Debug, Default)]
pub struct CoverageReport {
    pub total: usize,
    pub covered: usize,
    /// `Unsupported.context` -> count. Each top-level statement contributes its
    /// **first** blocker (the walk bails at the first unmodeled node), so this
    /// ranks "what to model next", not every gap in the subtree.
    pub unsupported: BTreeMap<&'static str, usize>,
}

/// Attempt extraction of each top-level statement independently and tally the
/// outcome. Unlike [`extract_facts_items`], one statement's `Unsupported` does not
/// abort the others — the point is to measure how far coverage reaches.
pub fn coverage_report(module: &Module) -> CoverageReport {
    let mut report = CoverageReport::default();
    for (body_idx, item) in module.body.iter().enumerate() {
        report.total += 1;
        let ordinal = source_unit_ordinal_for_statement(&module.body, StatementId(body_idx)).0;
        let mut extractor = Extractor::default();
        match extractor.module_item(item, ordinal) {
            Ok(_) => report.covered += 1,
            Err(Unsupported { context }) => *report.unsupported.entry(context).or_default() += 1,
        }
    }
    report
}
