//! Structured source-selector divergences. Matching constructs evidence;
//! presentation chooses the human adapter. Serde preserves the evidence.

use std::fmt::{self, Write};

use serde::{Deserialize, Serialize};
use source_match_holes::STRING_LITERAL_REGEX_PREDICATE;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ItemCategory {
    Statement,
    ModuleDeclaration,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ItemKind {
    Import,
    ExportDeclaration,
    NamedExport,
    DefaultDeclarationExport,
    DefaultExpressionExport,
    ExportAll,
    Block,
    Empty,
    Debugger,
    Return,
    Labeled,
    Break,
    Continue,
    If,
    Switch,
    Throw,
    Try,
    While,
    DoWhile,
    For,
    ForIn,
    ForOf,
    Declaration,
    Expression,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeclarationKind {
    Class,
    Function,
    Variable,
}

/// A syntax summary, not a snippet of already rendered diagnostic prose.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ExpressionShape {
    Identifier {
        name: String,
    },
    StringLiteralRegexPredicate,
    String {
        value: String,
    },
    Boolean {
        value: bool,
    },
    Null,
    /// ECMAScript numeric value's canonical spelling; kept textual so NaN and
    /// infinities do not acquire JSON's null representation.
    Number {
        value: String,
    },
    BigInt,
    Regex,
    JsxText,
    Super,
    Import,
    Call {
        callee: Box<Self>,
    },
    New {
        callee: Box<Self>,
    },
    Member {
        object: Box<Self>,
        property: MemberKey,
    },
    Object,
    Array,
    Arrow,
    Function,
    Class,
    Template,
    TaggedTemplate {
        tag: Box<Self>,
    },
    Expression,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum MemberKey {
    Named { name: String },
    Private { name: String },
    Computed,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum DeclaratorShape {
    Hole {
        name: String,
    },
    Binding {
        names: Vec<String>,
        initializer: Option<ExpressionShape>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct PinnedDeclarator {
    pub index: usize,
    pub declaration: DeclaratorShape,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum DeclaratorGap {
    NoPins,
    Leading {
        before: PinnedDeclarator,
        unmatched: Vec<DeclaratorShape>,
    },
    Between {
        left: PinnedDeclarator,
        right: PinnedDeclarator,
        unmatched: Vec<DeclaratorShape>,
    },
    Trailing {
        after: PinnedDeclarator,
        unmatched: Vec<DeclaratorShape>,
    },
    Unlocalized,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ClassMemberKey {
    Constructor,
    StaticBlock,
    Named { name: String },
    Computed,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "code", rename_all = "snake_case")]
pub enum MismatchReason {
    TopLevelItemKind {
        selector: ItemCategory,
        candidate: ItemCategory,
    },
    ItemKind {
        category: ItemCategory,
        selector: ItemKind,
        candidate: ItemKind,
    },
    ItemShape {
        category: ItemCategory,
    },
    DeclarationName {
        kind: DeclarationKind,
        selector: Option<String>,
        candidate: Option<String>,
    },
    FunctionSignatureOrBody,
    DeclarationKind {
        selector: DeclarationKind,
        candidate: DeclarationKind,
    },
    DeclarationShape,
    VariableKind {
        selector: Option<String>,
        candidate: Option<String>,
    },
    VariableShape,
    VariableCount {
        selector_count: usize,
        candidate_count: usize,
    },
    VariableDeclarator {
        index: usize,
        selector: DeclaratorShape,
        candidate: DeclaratorShape,
    },
    PinnedDeclaratorMissing {
        selector: PinnedDeclarator,
        remaining: Vec<DeclaratorShape>,
    },
    DeclaratorHolePlacement {
        gap: DeclaratorGap,
    },
    ClassMemberBody {
        member: ClassMemberKey,
    },
    ClassMemberMissing {
        member: ClassMemberKey,
    },
    ClassStructure,
}

impl MismatchReason {
    /// Ranking is a diagnostic heuristic, not an alternate matching decision.
    pub fn score(&self) -> usize {
        match self {
            Self::TopLevelItemKind { .. } => 1,
            Self::ItemKind { .. } => 10,
            Self::ItemShape { .. } => 20,
            Self::DeclarationKind { .. } => 30,
            Self::FunctionSignatureOrBody | Self::DeclarationShape | Self::VariableShape => 35,
            Self::DeclarationName { .. } => 40,
            Self::VariableKind { .. } | Self::ClassStructure => 45,
            Self::VariableCount { .. }
            | Self::VariableDeclarator { .. }
            | Self::PinnedDeclaratorMissing { .. }
            | Self::DeclaratorHolePlacement { .. } => 55,
            Self::ClassMemberBody { .. } => 65,
            Self::ClassMemberMissing { .. } => 70,
        }
    }

    /// Human text is composed only when a presentation consumer formats this
    /// adapter; serializing the reason never formats its explanation.
    pub fn display(&self) -> impl fmt::Display + '_ {
        MismatchDisplay(self)
    }
}

struct MismatchDisplay<'a>(&'a MismatchReason);

impl fmt::Display for MismatchDisplay<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.0 {
            MismatchReason::TopLevelItemKind {
                selector,
                candidate,
            } => write!(
                f,
                "top-level item kind differs: selector is {}, candidate is {}",
                category_label(*selector),
                category_label(*candidate)
            ),
            MismatchReason::ItemKind {
                category,
                selector,
                candidate,
            } => write!(
                f,
                "{} kind differs: selector is {}, candidate is {}",
                category_label(*category),
                item_label(*selector),
                item_label(*candidate)
            ),
            MismatchReason::ItemShape { category } => {
                write!(f, "{} shape differs", category_label(*category))
            }
            MismatchReason::DeclarationName {
                kind,
                selector,
                candidate,
            } => write!(
                f,
                "{} name differs: selector `{}`, candidate `{}`",
                declaration_label(*kind),
                selector.as_deref().unwrap_or_default(),
                candidate.as_deref().unwrap_or_default()
            ),
            MismatchReason::FunctionSignatureOrBody => {
                f.write_str("function signature or body differs")
            }
            MismatchReason::DeclarationKind {
                selector,
                candidate,
            } => write!(
                f,
                "declaration kind differs: selector is {}, candidate is {}",
                declaration_label(*selector),
                declaration_label(*candidate)
            ),
            MismatchReason::DeclarationShape => f.write_str("declaration shape differs"),
            MismatchReason::VariableKind {
                selector,
                candidate,
            } => write!(
                f,
                "variable declaration kind differs: selector is {}, candidate is {}",
                selector.as_deref().unwrap_or_default(),
                candidate.as_deref().unwrap_or_default()
            ),
            MismatchReason::VariableShape => f.write_str("variable declaration shape differs"),
            MismatchReason::VariableCount {
                selector_count,
                candidate_count,
            } => write!(
                f,
                "variable declarators differ: selector has {selector_count} declarator(s), candidate has {candidate_count}"
            ),
            MismatchReason::VariableDeclarator {
                index,
                selector,
                candidate,
            } => {
                write!(f, "variable declarator #{index} differs: selector ")?;
                declarator(f, selector)?;
                f.write_str(", candidate ")?;
                declarator(f, candidate)
            }
            MismatchReason::PinnedDeclaratorMissing {
                selector,
                remaining,
            } => {
                write!(f, "selector pinned declarator #{} ", selector.index)?;
                declarator(f, &selector.declaration)?;
                f.write_str(" was not found in order (")?;
                if remaining.is_empty() {
                    f.write_str("no candidate declarators remain")?;
                } else {
                    f.write_str("remaining candidate declarators: ")?;
                    declarators(f, remaining)?;
                }
                f.write_char(')')
            }
            MismatchReason::DeclaratorHolePlacement { gap } => gap_text(f, gap),
            MismatchReason::ClassMemberBody { member } => {
                f.write_str("class member `")?;
                class_member(f, member)?;
                f.write_str("` matched by name, but its signature or body differs")
            }
            MismatchReason::ClassMemberMissing { member } => {
                f.write_str("selector class pinned member `")?;
                class_member(f, member)?;
                f.write_str("` was not found in the candidate class body in order")
            }
            MismatchReason::ClassStructure => {
                f.write_str("class heritage, decorators, or member order differs")
            }
        }
    }
}

fn category_label(value: ItemCategory) -> &'static str {
    match value {
        ItemCategory::Statement => "statement",
        ItemCategory::ModuleDeclaration => "module declaration",
    }
}

fn declaration_label(value: DeclarationKind) -> &'static str {
    match value {
        DeclarationKind::Class => "class",
        DeclarationKind::Function => "function",
        DeclarationKind::Variable => "variable",
    }
}

fn item_label(value: ItemKind) -> &'static str {
    match value {
        ItemKind::Import => "import",
        ItemKind::ExportDeclaration => "export declaration",
        ItemKind::NamedExport => "named export",
        ItemKind::DefaultDeclarationExport => "default declaration export",
        ItemKind::DefaultExpressionExport => "default expression export",
        ItemKind::ExportAll => "export all",
        ItemKind::Block => "block",
        ItemKind::Empty => "empty",
        ItemKind::Debugger => "debugger",
        ItemKind::Return => "return",
        ItemKind::Labeled => "labeled",
        ItemKind::Break => "break",
        ItemKind::Continue => "continue",
        ItemKind::If => "if",
        ItemKind::Switch => "switch",
        ItemKind::Throw => "throw",
        ItemKind::Try => "try",
        ItemKind::While => "while",
        ItemKind::DoWhile => "do while",
        ItemKind::For => "for",
        ItemKind::ForIn => "for in",
        ItemKind::ForOf => "for of",
        ItemKind::Declaration => "declaration",
        ItemKind::Expression => "expression",
    }
}

fn class_member(f: &mut fmt::Formatter<'_>, value: &ClassMemberKey) -> fmt::Result {
    match value {
        ClassMemberKey::Constructor => f.write_str("constructor"),
        ClassMemberKey::StaticBlock => f.write_str("static block"),
        ClassMemberKey::Computed => f.write_str("<computed>"),
        ClassMemberKey::Named { name } => f.write_str(name),
    }
}

fn declarator(f: &mut fmt::Formatter<'_>, value: &DeclaratorShape) -> fmt::Result {
    f.write_char('`')?;
    match value {
        DeclaratorShape::Hole { name } => f.write_str(name)?,
        DeclaratorShape::Binding { names, initializer } => {
            if names.is_empty() {
                f.write_str("<pattern>")?;
            } else {
                f.write_str(&names.join("/"))?;
            }
            if let Some(initializer) = initializer {
                f.write_str(" = ")?;
                expression(f, initializer)?;
            }
        }
    }
    f.write_char('`')
}

fn declarators(f: &mut fmt::Formatter<'_>, values: &[DeclaratorShape]) -> fmt::Result {
    if values.is_empty() {
        return f.write_str("<none>");
    }
    const LIMIT: usize = 10;
    for (index, value) in values.iter().take(LIMIT).enumerate() {
        if index > 0 {
            f.write_str(", ")?;
        }
        declarator(f, value)?;
    }
    if values.len() > LIMIT {
        write!(f, ", ... +{} more", values.len() - LIMIT)?;
    }
    Ok(())
}

fn expression(f: &mut fmt::Formatter<'_>, value: &ExpressionShape) -> fmt::Result {
    match value {
        ExpressionShape::Identifier { name } => f.write_str(name),
        ExpressionShape::StringLiteralRegexPredicate => {
            write!(f, "{STRING_LITERAL_REGEX_PREDICATE}(...)")
        }
        ExpressionShape::String { value } => write!(f, "\"{value}\""),
        ExpressionShape::Boolean { value } => write!(f, "{value}"),
        ExpressionShape::Null => f.write_str("null"),
        ExpressionShape::Number { value } => f.write_str(value),
        ExpressionShape::BigInt => f.write_str("0n"),
        ExpressionShape::Regex => f.write_str("/.../"),
        ExpressionShape::JsxText => f.write_str("jsx-text"),
        ExpressionShape::Super => f.write_str("super"),
        ExpressionShape::Import => f.write_str("import"),
        ExpressionShape::Call { callee } => {
            expression(f, callee)?;
            f.write_str("(...)")
        }
        ExpressionShape::New { callee } => {
            f.write_str("new ")?;
            expression(f, callee)?;
            f.write_str("(...)")
        }
        ExpressionShape::Member { object, property } => {
            expression(f, object)?;
            match property {
                MemberKey::Named { name } => write!(f, ".{name}"),
                MemberKey::Private { name } => write!(f, ".#{name}"),
                MemberKey::Computed => f.write_str("[...]"),
            }
        }
        ExpressionShape::Object => f.write_str("{...}"),
        ExpressionShape::Array => f.write_str("[...]"),
        ExpressionShape::Arrow => f.write_str("(...) => ..."),
        ExpressionShape::Function => f.write_str("function(...)"),
        ExpressionShape::Class => f.write_str("class {...}"),
        ExpressionShape::Template => f.write_str("`...`"),
        ExpressionShape::TaggedTemplate { tag } => {
            expression(f, tag)?;
            f.write_str(" `...`")
        }
        ExpressionShape::Expression => f.write_str("expression"),
    }
}

fn gap_text(f: &mut fmt::Formatter<'_>, gap: &DeclaratorGap) -> fmt::Result {
    match gap {
        DeclaratorGap::NoPins => f.write_str("pinned declarators matched in order, but DECLARATORS_* hole placement differed"),
        DeclaratorGap::Unlocalized => f.write_str("pinned declarators matched in order, but DECLARATORS_* hole placement differed. Check that each unrelated sibling declarator is covered by a hole before, after, or between pinned declarators."),
        DeclaratorGap::Leading { before, unmatched } => {
            write!(f, "candidate has unmatched leading declarator(s) before selector declarator #{} ", before.index)?;
            declarator(f, &before.declaration)?; f.write_str(": ")?; declarators(f, unmatched)?;
            f.write_str(". Add a `DECLARATORS_* = null` pseudo-declarator before the first pinned declarator.")
        }
        DeclaratorGap::Between { left, right, unmatched } => {
            write!(f, "candidate has unmatched declarator(s) between selector declarator #{} ", left.index)?;
            declarator(f, &left.declaration)?; write!(f, " and #{} ", right.index)?;
            declarator(f, &right.declaration)?; f.write_str(": ")?; declarators(f, unmatched)?;
            f.write_str(". Add a `DECLARATORS_* = null` pseudo-declarator between those pinned declarators.")
        }
        DeclaratorGap::Trailing { after, unmatched } => {
            write!(f, "candidate has unmatched trailing declarator(s) after selector declarator #{} ", after.index)?;
            declarator(f, &after.declaration)?; f.write_str(": ")?; declarators(f, unmatched)?;
            f.write_str(". Add a `DECLARATORS_* = null` pseudo-declarator after the last pinned declarator.")
        }
    }
}
