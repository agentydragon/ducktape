use crate::{ClassSurface, ClassValueFacts, NamespaceValueFacts, ValueExpr};
use analysis_vocabulary::value::Kind;
use inference::{FactContext, IterativeFacts, IterativeInference, IterativeRules};
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::*;

/// Class provenance is reachability, not a traversal of assignment paths.
/// Each edge reads one Boolean fact, so a shared tail is evaluated once per
/// incoming edge and an unseeded binding cycle stays false at completion.
pub(crate) struct LocalClassProvenance {
    edges: Vec<(Id, Id)>,
    #[cfg(test)]
    expression_visits: usize,
}

impl LocalClassProvenance {
    fn compile(facts: &NamespaceValueFacts) -> (Self, BTreeSet<Id>) {
        let mut rules = Self {
            edges: Vec::new(),
            #[cfg(test)]
            expression_visits: 0,
        };
        let mut seeds = BTreeSet::new();
        for (target, values) in &facts.binding_assignments {
            for value in values {
                rules.collect(target, value, facts, &mut seeds);
            }
        }
        (rules, seeds)
    }

    fn collect(
        &mut self,
        target: &Id,
        value: &ValueExpr,
        facts: &NamespaceValueFacts,
        seeds: &mut BTreeSet<Id>,
    ) {
        #[cfg(test)]
        {
            self.expression_visits += 1;
        }
        match value {
            ValueExpr::Alloc(Kind::Class, _) => {
                seeds.insert(target.clone());
            }
            ValueExpr::Binding(source) => self.edges.push((target.clone(), source.clone())),
            ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } => {
                for alternative in value.result_alternatives().expect("alternative expression") {
                    self.collect(target, alternative, facts, seeds);
                }
            }
            ValueExpr::Member(object, _, _, _) => {
                // A uniquely resolved instance also has a class surface. Other
                // receivers contribute precisely their possible class values.
                if facts.resolve_class(object, &mut BTreeSet::new()).is_some() {
                    seeds.insert(target.clone());
                } else {
                    self.collect(target, object, facts, seeds);
                }
            }
            _ => {}
        }
    }

    fn solve(self, seeds: BTreeSet<Id>) -> IterativeFacts<Id, ()> {
        let edge_count = self.edges.len();
        let mut inference = IterativeInference::new(self);
        for binding in seeds {
            inference.seed(binding, ());
        }
        inference.solve_values_resumable(0..edge_count)
    }
}

impl IterativeRules for LocalClassProvenance {
    type Key = Id;
    type Fact = ();
    type Rule = usize;
    type MergeScratch = ();
    const SOURCE_ONLY: bool = true;

    fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Id, ()>) {
        let (target, source) = &self.edges[*rule];
        if context.read(source.clone()).is_some() {
            context.propose(target.clone(), ());
        }
    }

    fn merge(_: &Id, _: &mut (), _: ()) -> bool {
        false
    }
}

impl NamespaceValueFacts {
    pub fn class_allocation_without_user_code(&self, span: u32) -> bool {
        self.classes
            .get(&span)
            .is_some_and(|class| class.allocation_without_user_code)
    }

    pub fn has_instance_field(&self, span: u32, property: &str) -> bool {
        self.classes
            .get(&span)
            .is_some_and(|class| class.instance_fields.contains_key(property))
    }

    pub fn from_assignments(
        classes: BTreeMap<u32, ClassValueFacts>,
        verified_data_objects: BTreeSet<u32>,
        assignments: &[&(Id, ValueExpr)],
    ) -> Self {
        let mut values = BTreeMap::<Id, Vec<ValueExpr>>::new();
        for (binding, value) in assignments {
            values
                .entry(binding.clone())
                .or_default()
                .push(value.clone());
        }
        let mut facts = Self {
            classes,
            verified_data_objects,
            bindings: values
                .iter()
                .filter(|(_, values)| values.len() == 1)
                .map(|(binding, values)| (binding.clone(), values[0].clone()))
                .collect(),
            binding_assignments: values,
            unstable_class_bindings: BTreeSet::new(),
        };
        let (class_provenance, class_seeds) = LocalClassProvenance::compile(&facts);
        let class_bindings = class_provenance.solve(class_seeds);
        facts.unstable_class_bindings = facts
            .binding_assignments
            .iter()
            .filter_map(|(binding, values)| {
                let may_be_class = class_bindings.get(binding).is_some();
                let uniquely_resolved = values.len() == 1
                    && facts
                        .resolve_class(&values[0], &mut BTreeSet::new())
                        .is_some();
                (may_be_class && !uniquely_resolved).then(|| binding.clone())
            })
            .collect();
        facts
    }

    pub(crate) fn own_data_member<'a>(
        &'a self,
        object: &'a ValueExpr,
        property: &str,
        id: &Id,
        aliases: &BTreeSet<Id>,
    ) -> Option<&'a ValueExpr> {
        let ValueExpr::ObjectLiteral(span, properties) = self.unique_literal(object)? else {
            return None;
        };
        if !self.verified_data_objects.contains(span) {
            return None;
        }
        // A container with no explicit namespace path keeps the prior
        // conservative receiver rule. Its field might gain an accessor or
        // other value through a call without publishing this namespace.
        if !properties
            .iter()
            .any(|(_, value)| value.carries_namespace(id, aliases))
        {
            return None;
        }
        let mut values = properties
            .iter()
            .filter(|(name, _)| name.as_deref() == Some(property))
            .map(|(_, value)| value);
        let value = values.next()?;
        values.next().is_none().then_some(value)
    }

    pub(crate) fn unique_literal<'a>(&'a self, object: &'a ValueExpr) -> Option<&'a ValueExpr> {
        let object = match object {
            ValueExpr::Binding(binding) => self.bindings.get(binding)?,
            value => value,
        };
        matches!(object, ValueExpr::ObjectLiteral(_, _)).then_some(object)
    }

    pub fn resolve_class(
        &self,
        value: &ValueExpr,
        visiting: &mut BTreeSet<Id>,
    ) -> Option<(u32, ClassSurface)> {
        match value {
            ValueExpr::Alloc(Kind::Class, span) => Some((*span, ClassSurface::Constructor)),
            ValueExpr::Binding(binding) => {
                if !visiting.insert(binding.clone()) {
                    return None;
                }
                let result = self
                    .bindings
                    .get(binding)
                    .and_then(|value| self.resolve_class(value, visiting));
                visiting.remove(binding);
                result
            }
            ValueExpr::New(callee, _, _) => self
                .resolve_class(callee, visiting)
                .filter(|(_, surface)| *surface == ClassSurface::Constructor)
                .map(|(span, _)| (span, ClassSurface::Instance)),
            _ => None,
        }
    }

    pub(crate) fn class_may_publish(
        &self,
        value: &ValueExpr,
        id: &Id,
        aliases: &BTreeSet<Id>,
        visiting_bindings: &mut BTreeSet<Id>,
        visiting_classes: &mut BTreeSet<(u32, ClassSurface)>,
    ) -> bool {
        let Some((span, surface)) = self.resolve_class(value, &mut BTreeSet::new()) else {
            return false;
        };
        if !visiting_classes.insert((span, surface)) {
            return true;
        }
        let result = self.classes.get(&span).is_none_or(|class| {
            let (opaque, fields) = match surface {
                ClassSurface::Constructor => (class.opaque_static, &class.static_fields),
                ClassSurface::Instance => (class.opaque_instance, &class.instance_fields),
            };
            let inherited_static_may_publish = if surface == ClassSurface::Constructor && !opaque {
                class.super_class.as_ref().is_some_and(|super_class| {
                    let Some((base_span, ClassSurface::Constructor)) =
                        self.resolve_class(super_class, &mut BTreeSet::new())
                    else {
                        return true;
                    };
                    self.class_may_publish(
                        &ValueExpr::Alloc(Kind::Class, base_span),
                        id,
                        aliases,
                        visiting_bindings,
                        visiting_classes,
                    )
                })
            } else {
                false
            };
            opaque
                || inherited_static_may_publish
                || fields.values().any(|field| {
                    field.may_publish_namespace_inner(
                        id,
                        aliases,
                        self,
                        visiting_bindings,
                        visiting_classes,
                    )
                })
        });
        visiting_classes.remove(&(span, surface));
        result
    }
}

#[cfg(test)]
mod local_class_provenance_tests {
    use super::*;

    fn facts(source: &str) -> NamespaceValueFacts {
        swc_common::GLOBALS.set(&swc_common::Globals::new(), || {
            let module = js_ast::parse_js_module("class-provenance.js", source)
                .expect("valid provenance fixture")
                .module;
            let assignments = module
                .body
                .iter()
                .flat_map(|item| {
                    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) = item else {
                        panic!("fixture contains only variable declarations");
                    };
                    declaration.decls.iter().map(|declaration| {
                        let Pat::Ident(binding) = &declaration.name else {
                            panic!("fixture uses named bindings");
                        };
                        (
                            binding.to_id(),
                            ValueExpr::from_expr(declaration.init.as_deref().expect("initializer")),
                        )
                    })
                })
                .collect::<Vec<_>>();
            NamespaceValueFacts::from_assignments(
                BTreeMap::new(),
                BTreeSet::new(),
                &assignments.iter().collect::<Vec<_>>(),
            )
        })
    }

    fn assert_classes(source: &str, expected: &[&str], max_visits: usize) {
        let facts = facts(source);
        let (rules, seeds) = LocalClassProvenance::compile(&facts);
        assert!(rules.expression_visits <= max_visits);
        let edges = rules.edges.len();
        let result = rules.solve(seeds);
        // A Boolean source changes at most once, so each edge runs initially
        // and at most once after its source appears, even in shared cycles.
        assert!(result.evaluations() <= 2 * edges);
        let names = result
            .keys()
            .map(|binding| binding.0.as_ref())
            .collect::<BTreeSet<_>>();
        assert_eq!(names, expected.iter().copied().collect());
    }

    #[test]
    #[ignore = "run //devinfra/js/debundle/e2e/bench:analysis_work_bounds_test"]
    fn work_bounds_shared_chains_and_diamonds() {
        for (depth, root) in [(4, "{}"), (8, "{}"), (16, "{}"), (16, "class {}")] {
            let mut source = format!("var root = {root};");
            let mut names = vec!["root".to_owned()];
            for index in 1..=depth {
                let prior = if index == 1 {
                    "root".to_owned()
                } else {
                    format!("join{}", index - 1)
                };
                source.push_str(&format!(
                    "var left{index} = flag ? {prior} : {prior};\n\
                     var right{index} = {prior};\n\
                     var join{index} = flag ? left{index} : right{index};\n"
                ));
                names.extend([
                    format!("left{index}"),
                    format!("right{index}"),
                    format!("join{index}"),
                ]);
            }
            let expected = if root == "{}" {
                vec![]
            } else {
                names.iter().map(String::as_str).collect()
            };
            assert_classes(&source, &expected, 1 + 7 * depth);
        }
    }

    #[test]
    fn cycles_require_a_reachable_class_seed() {
        assert_classes("var a = b; var b = flag ? a : c; var c = a;", &[], 5);
        // The class seed follows the back-edge alternative. An in-progress
        // false answer for b or c must not become a completed negative.
        assert_classes(
            "var a = flag ? b : class {}; var b = c; var c = a; var tail = b;",
            &["a", "b", "c", "tail"],
            6,
        );
    }

    #[test]
    fn class_surfaces_and_opaque_value_variants_keep_their_meaning() {
        let source = "var constructor = class {};\n\
            var instance = new constructor();\n\
            var constructorMember = constructor.member;\n\
            var instanceMember = instance.member;\n\
            var conditional = flag ? constructor : {};\n\
            var member = conditional.member;\n\
            var testOnly = constructor ? {} : {};\n\
            var container = { constructor };\n\
            var sequence = (0, constructor);\n\
            var call = constructor();\n\
            var functionValue = function () {};\n\
            var rebound = constructor; var rebound = {};";
        assert_classes(
            source,
            &[
                "constructor",
                "constructorMember",
                "instanceMember",
                "conditional",
                "member",
                "rebound",
            ],
            20,
        );
        let unstable = facts(source)
            .unstable_class_bindings
            .into_iter()
            .map(|binding| binding.0.to_string())
            .collect::<BTreeSet<_>>();
        assert_eq!(
            unstable,
            [
                "constructorMember",
                "instanceMember",
                "conditional",
                "member",
                "rebound"
            ]
            .into_iter()
            .map(str::to_owned)
            .collect()
        );
    }
}
