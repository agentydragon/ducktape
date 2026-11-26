//! Closed source declarations, separate from possible values and reached prefixes.

use super::*;

/// Eligibility to publish a body summary is not evidence that a particular
/// read reached an initialized cell. Class declarations remain evaluation-time
/// definitions; their TDZ obligations are enforced by ordered execution/gates.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallableDeclaration {
    Function(u32),
    Class(u32),
    Initializer { start: u32, passive_provider: bool },
}

impl CallableDeclaration {
    pub fn start(self) -> u32 {
        match self {
            Self::Function(start) | Self::Class(start) | Self::Initializer { start, .. } => start,
        }
    }

    pub fn is_declaration(self) -> bool {
        matches!(self, Self::Function(_) | Self::Class(_))
    }

    pub fn permits_foreign_summary(self, captures_empty: bool) -> bool {
        match self {
            Self::Function(_) | Self::Class(_) => true,
            Self::Initializer {
                passive_provider, ..
            } => passive_provider && captures_empty,
        }
    }

    pub(super) fn rekey(&mut self, site: impl FnOnce(u32) -> u32) {
        match self {
            Self::Function(start) | Self::Class(start) | Self::Initializer { start, .. } => {
                *start = site(*start);
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct DeclarationInitialization {
    /// A direct single-binding let/const owner. Uniqueness of this binding's
    /// declaration owner is checked only after the source census is complete.
    lexical_read_guard: bool,
    pub(super) callable: Option<CallableDeclaration>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct InitializationSites(BTreeMap<SourceUnitOrdinal, DeclarationInitialization>);

impl InitializationSites {
    pub(super) fn union_with(&mut self, other: Self) -> bool {
        let before = self.0.len();
        for (owner, declaration) in other.0 {
            if let Some(existing) = self.0.insert(owner, declaration) {
                assert_eq!(existing, declaration, "immutable source declaration");
            }
        }
        self.0.len() != before
    }

    pub(super) fn lexical_read_guard(&self) -> Option<SourceUnitOrdinal> {
        let mut declarations = self.0.iter();
        let (owner, declaration) = declarations.next()?;
        (declarations.next().is_none() && declaration.lexical_read_guard).then_some(*owner)
    }

    pub(super) fn callables(&self) -> impl Iterator<Item = CallableDeclaration> + '_ {
        self.0
            .values()
            .filter_map(|declaration| declaration.callable)
    }

    pub(super) fn rekey(&mut self, mut site: impl FnMut(u32) -> u32) {
        for declaration in self.0.values_mut() {
            if let Some(callable) = &mut declaration.callable {
                callable.rekey(&mut site);
            }
        }
    }
}

#[derive(Default)]
pub(super) struct InitializationCensus {
    declarations: BTreeMap<BindingSlot, InitializationSites>,
    /// Every emitted owner contains only inert declarations and imports of
    /// this provider's other owners. No owner can reenter an importer before
    /// its imported callable has initialized, even inside a provider-local SCC.
    active_provider: bool,
}

impl InitializationCensus {
    pub(super) fn record(
        &mut self,
        item: &ModuleItem,
        declared: &BTreeSet<Id>,
        owner: SourceUnitOrdinal,
        symbols: &mut FlowSymbols,
    ) {
        let declaration = match item {
            ModuleItem::Stmt(Stmt::Decl(declaration)) => Some(declaration),
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => Some(&export.decl),
            _ => None,
        };
        let variable = super::super::item_views::var_decl_of_item(item);
        let single_variable = variable.and_then(|variable| {
            let [declarator] = variable.decls.as_slice() else {
                return None;
            };
            let Pat::Ident(binding) = &declarator.name else {
                return None;
            };
            Some((variable, declarator, binding.id.to_id()))
        });
        let lexical_read_guard = single_variable
            .as_ref()
            .is_some_and(|(variable, _, binding)| {
                matches!(variable.kind, VarDeclKind::Let | VarDeclKind::Const)
                    && !variable.declare
                    && declared.len() == 1
                    && declared.contains(binding)
            });
        let callable = match declaration {
            Some(Decl::Fn(function)) => {
                Some(CallableDeclaration::Function(function.function.span.lo.0))
            }
            Some(Decl::Class(class)) => Some(CallableDeclaration::Class(class.class.span.lo.0)),
            Some(Decl::Var(_)) => single_variable.as_ref().and_then(|(_, declarator, _)| {
                let start = match declarator.init.as_deref()?.unwrap_parens() {
                    Expr::Fn(function) => function.function.span.lo.0,
                    Expr::Arrow(arrow) => arrow.span.lo.0,
                    _ => return None,
                };
                Some(CallableDeclaration::Initializer {
                    start,
                    passive_provider: false,
                })
            }),
            _ => None,
        };
        for binding in declared {
            self.declarations
                .entry(symbols.binding(binding.clone()))
                .or_default()
                .0
                .insert(
                    owner,
                    DeclarationInitialization {
                        lexical_read_guard,
                        callable,
                    },
                );
        }
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(_)))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                decl: Decl::Var(_),
                ..
            })) => {
                self.active_provider |=
                    !single_variable.is_some_and(|(variable, declarator, _)| {
                        variable.kind == VarDeclKind::Const
                            && !variable.declare
                            && declarator.init.as_deref().is_some_and(|initializer| {
                                matches!(
                                    initializer.unwrap_parens(),
                                    Expr::Fn(_)
                                        | Expr::Arrow(_)
                                        | Expr::Lit(
                                            Lit::Str(_)
                                                | Lit::Bool(_)
                                                | Lit::Null(_)
                                                | Lit::Num(_)
                                                | Lit::BigInt(_)
                                        )
                                )
                            })
                    });
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) if export.src.is_none() => {
                for specifier in &export.specifiers {
                    if !matches!(specifier, ExportSpecifier::Named(named)
                        if matches!(named.orig, ModuleExportName::Ident(_)))
                    {
                        self.active_provider = true;
                    }
                }
            }
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_)))
            | ModuleItem::Stmt(Stmt::Empty(_))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                decl: Decl::Fn(_),
                ..
            })) => {}
            _ => self.active_provider = true,
        }
    }

    pub(super) fn finish(mut self) -> Vec<FlowEvent> {
        if !self.active_provider {
            for declaration in self
                .declarations
                .values_mut()
                .flat_map(|sites| sites.0.values_mut())
            {
                if let Some(CallableDeclaration::Initializer {
                    passive_provider, ..
                }) = &mut declaration.callable
                {
                    *passive_provider = true;
                }
            }
        }
        self.declarations
            .into_iter()
            .map(|(id, sites)| FlowEvent::Initialization { id, sites })
            .collect()
    }
}
