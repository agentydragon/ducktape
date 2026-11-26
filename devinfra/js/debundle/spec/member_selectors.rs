use crate::BindingSourceKind;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Member {
    /// Public export name. Defaults to the bound `selector.binding.name`.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub name: Option<String>,
    /// Raw authored selector syntax. Keep this permissive so invalid YAML can
    /// still be listed and repaired; semantic code validates it before use.
    pub selector: RawMemberSelector,
}

/// The permissive selector shape stored in module YAML. It preserves missing
/// and conflicting selector keys for validation diagnostics and repair tools.
#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RawMemberSelector {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding: Option<BindingSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cross_ref: Option<CrossRefSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reads_member: Option<ReadsMemberSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub member_of_module: Option<MemberOfModuleSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub passed_to_call: Option<PassedToCallSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub makes_decorate_call: Option<MakesDecorateCallSelector>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub intrinsic_alias: Option<IntrinsicAliasSelector>,
}

/// Pin a member by referencing another spec member instead of by this member's
/// own (re-minify-fragile) minified name. The anchor names another member by its
/// readable name; the target is resolved through the dependency graph as the
/// entity that references the anchor's resolved binding — e.g. a shapeless
/// delegator `function T(x){ return Anchor(x) }` pinned as "the function that
/// references @Anchor".
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct CrossRefSelector {
    /// The target references the anchor member (a delegator / consumer body).
    pub references: String,
    /// Optional statement-kind constraint disambiguating when several source_units
    /// stand in the relation to the anchor (e.g. `function_declaration`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// Pin a member by the **member it reads** off an object, instead of by this
/// member's own (re-minify-fragile) minified name. The canonical shape is a TS
/// codegen helper `function ls(c){ return c.uniqueId }` whose stable identity is
/// "the function that reads `.uniqueId` off the codegen context" — pinned by the
/// invariant property name `.uniqueId` (and optionally the object it reads off),
/// never by the minified `ls`. Resolved through the dependency graph's `reads_member`
/// EDB: the unique declaring source_unit whose body reads the named member.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct ReadsMemberSelector {
    /// The property name `X` the target reads (`obj.X`). Required — the relation
    /// is "reads member `.member`".
    pub member: String,
    /// Optional object constraint: the readable `name:` of another member the
    /// property is read off (`@object.member`). Narrows "the source_unit that reads
    /// `.X`" to "the source_unit that reads `.X` **off `@object`**" — the codegen
    /// context being the canonical object. Resolved like a `cross_ref` anchor:
    /// the object's already-resolved minified binding rides the relational edge.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub object: Option<String>,
    /// Optional statement-kind constraint disambiguating when several source_units read
    /// the member (e.g. `function_declaration`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// Pin a member by **how it is consumed at a use site** — the export consumed as
/// `mod.member`, where `mod` is a binding imported from `module` — instead of by
/// this member's own (re-minify-fragile) minified name. This is the first
/// *use-site* selector: it rides the import/use graph rather than the target's
/// own body. The canonical shapes are the empty-class/superclass cluster
/// (`class Uee extends Ye {}`, several byte-identical empty subclasses
/// distinguished only by *how each is consumed*) and shapeless delegators with no
/// internal anchor. Both `module` (an import specifier) and `member` (an export
/// name) are re-minify-invariant, so the whole edge survives a bundle rebuild.
/// Resolved through the dependency graph's `member_of_module` EDB: the unique declaring
/// source_unit whose body consumes `<module>.<member>`.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct MemberOfModuleSelector {
    /// The import **source specifier** the consumed binding is imported from
    /// (`"./codegen"`, `"react"`). Required — half of the invariant "consumed as
    /// `module.member`" identity.
    pub module: String,
    /// The export **name** consumed off the imported binding (`mod.member`).
    /// Required — the other half of the identity.
    pub member: String,
    /// Optional statement-kind constraint disambiguating when several source_units
    /// consume the module member (e.g. `class_declaration` for the empty-subclass
    /// cluster).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// Pin a member by it being **passed as an argument** to a call of a known callee
/// instead of by this member's own (re-minify-fragile) minified name. The
/// canonical shape is a registry-style target: a top-level `class FooAccessor {}`
/// (often empty or otherwise shapeless) whose only stable identity is an
/// external `registry.register(FooAccessor)` statement. The target *is the
/// argument*; the call that names it lives elsewhere.
/// This is the inverse direction of [`MemberOfModuleSelector`], which pins the
/// source_unit whose *own* subtree consumes `mod.X` and explicitly cannot reach a target
/// distinguished only by such an external registration. `callee_member` (the call's
/// method name) and the registry's own stable identity are re-minify-invariant, so
/// the edge survives a bundle rebuild. Resolved through the dependency graph's
/// `passed_to_call` EDB: the unique declaring source_unit whose binding is the call
/// argument.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct PassedToCallSelector {
    /// The callee **member name** `.method` of the call the target is passed to
    /// (`registry.register(Target)` ⟹ `callee_member: register`). Required — the
    /// relation is "passed to a call of `.callee_member`".
    pub callee_member: String,
    /// Optional callee **object** constraint: the readable `name:` of another
    /// member that is the call's receiver (`@registry.register(...)`). Narrows "the
    /// target passed to `.register`" to "the target passed to `@registry.register`"
    /// — the registry singleton being the canonical object. Resolved like a
    /// `cross_ref` / `reads_member` anchor: the object's already-resolved minified
    /// binding rides the relational edge.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub object: Option<String>,
    /// Optional **argument position** constraint (0-based): which argument of the
    /// call the target occupies (`h.define("widget", Target)` ⟹ `arg_index: 1`).
    /// Absent ⟹ any position. Narrows when one callee takes the target at a fixed
    /// slot alongside other arguments.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub arg_index: Option<usize>,
    /// Optional statement-kind constraint disambiguating when several source_units are
    /// passed to the callee member (e.g. `class_declaration` for the registry
    /// empty-subclass cluster).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// `makes_decorate_call` EDB: the unique declaring source_unit whose binding is the
/// **callee** of an esbuild/TypeScript `__decorate`-style decorator application.
/// The inverse direction of `passed_to_call` (the target *makes* the call rather
/// than being *passed to* it). The byte-identical-across-modules helper copies have
/// no anchor in their own body; this selector pins each by the **class it
/// decorates** — a separately-pinned entity — so the edge survives a rebuild
/// while the helper's minified name does not.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct MakesDecorateCallSelector {
    /// The decorated class: the readable `name:` of another member that is the base
    /// of the decorator application's 2nd argument (`C.prototype` or bare `C`).
    /// Required — it is the re-minify-invariant anchor the selector rides ("the
    /// helper that decorates `@ClassAnchor`"). Resolved like a `passed_to_call`
    /// object anchor: the class's already-resolved minified binding rides the edge.
    pub class: String,
    /// Optional decorated **member name** (the 3rd-argument string literal,
    /// `"isVisible"`). Narrows "the helper that decorates `@C`" to "the helper that
    /// decorates `@C`'s `isVisible`" when needed. Absent ⟹ any decorated member of
    /// the class (the common case — one helper decorates many members of one class).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub member: Option<String>,
    /// Optional statement-kind constraint. The esbuild decorate helper is always a
    /// `variable_declarator`; the constraint narrows past any non-var source_unit that
    /// spuriously shares the class anchor on a full bundle.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<BindingSourceKind>,
}

/// Pin a member that is an **intrinsic-method alias off the unshadowed global
/// `Object`** (`var X = Object.defineProperty` / `var X =
/// Object.getOwnPropertyDescriptor`) by the helper that **references** it, instead
/// of by this member's own (re-minify-fragile) minified name. These are the esbuild
/// decorate-trio's two companions: byte-identical across modules, with no anchor in
/// their own body — no `source_match` can pin them (N identical copies, the anchor
/// is the global `Object`, not a spec member). The one re-minify-invariant edge each
/// carries is that it is read **only inside** its trio's `__decorate` helper body, so
/// the selector pairs the structural recognition of `var X = Object.<property>` with
/// an inverse-`references` edge to the helper. `referenced_by` names that helper —
/// now a stable `@Name` because `makes_decorate_call` pins it; the intrinsic
/// `property` is a spec-level method name the bundler does not rewrite. Resolved
/// through the dependency graph's `intrinsic_alias` EDB: the unique declaring source_unit whose
/// binding is the `Object.<property>` alias referenced by the helper.
#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct IntrinsicAliasSelector {
    /// The intrinsic method name aliased off the global `Object` (`defineProperty`,
    /// `getOwnPropertyDescriptor`). Required — the re-minify-invariant label half of
    /// the relation ("the `Object.<property>` alias").
    pub property: String,
    /// The helper that references this alias: the readable `name:` of another member
    /// (the trio's `__decorate` helper, itself pinned by `makes_decorate_call`).
    /// Required — the disambiguating anchor that picks the unique copy among the N
    /// byte-identical aliases. Resolved like a `makes_decorate_call` class anchor:
    /// the helper's already-resolved minified binding rides the `references` edge.
    pub referenced_by: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, Eq, PartialEq, Ord, PartialOrd)]
#[serde(deny_unknown_fields)]
pub struct BindingSelector {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(default)]
    pub kind: Option<BindingSourceKind>,
}

impl RawMemberSelector {
    /// Normalize authored selector syntax into the existing semantic union.
    /// This is intentionally fallible: callers that edit specs can retain the
    /// raw value after reporting the error, while semantic consumers require
    /// the returned `MemberSelectorSpec`.
    pub fn validate(&self) -> std::result::Result<MemberSelectorSpec, MemberSelectorError> {
        match (
            &self.binding,
            &self.cross_ref,
            &self.reads_member,
            &self.member_of_module,
            &self.passed_to_call,
            &self.makes_decorate_call,
            &self.intrinsic_alias,
        ) {
            (Some(binding), None, None, None, None, None, None) => {
                Ok(MemberSelectorSpec::Binding(binding.clone()))
            }
            (None, Some(cross_ref), None, None, None, None, None) => {
                Ok(MemberSelectorSpec::CrossRef(cross_ref.clone()))
            }
            (None, None, Some(reads_member), None, None, None, None) => {
                Ok(MemberSelectorSpec::ReadsMember(reads_member.clone()))
            }
            (None, None, None, Some(member_of_module), None, None, None) => {
                Ok(MemberSelectorSpec::MemberOfModule(member_of_module.clone()))
            }
            (None, None, None, None, Some(passed_to_call), None, None) => {
                Ok(MemberSelectorSpec::PassedToCall(passed_to_call.clone()))
            }
            (None, None, None, None, None, Some(makes_decorate_call), None) => Ok(
                MemberSelectorSpec::MakesDecorateCall(makes_decorate_call.clone()),
            ),
            (None, None, None, None, None, None, Some(intrinsic_alias)) => {
                Ok(MemberSelectorSpec::IntrinsicAlias(intrinsic_alias.clone()))
            }
            (None, None, None, None, None, None, None) => Err(MemberSelectorError::MissingSelector),
            _ => Err(MemberSelectorError::MultipleSelectors),
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub enum MemberSelectorSpec {
    Binding(BindingSelector),
    CrossRef(CrossRefSelector),
    ReadsMember(ReadsMemberSelector),
    MemberOfModule(MemberOfModuleSelector),
    PassedToCall(PassedToCallSelector),
    MakesDecorateCall(MakesDecorateCallSelector),
    IntrinsicAlias(IntrinsicAliasSelector),
}

#[derive(
    Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize, thiserror::Error,
)]
#[serde(rename_all = "snake_case")]
pub enum MemberSelectorError {
    #[error(
        "members[].selector must include one of `binding`, `cross_ref`, \
             `reads_member`, `member_of_module`, `passed_to_call`, \
             `makes_decorate_call`, or `intrinsic_alias`"
    )]
    MissingSelector,
    #[error(
        "members[].selector must use exactly one of `binding`, `cross_ref`, \
             `reads_member`, `member_of_module`, `passed_to_call`, \
             `makes_decorate_call`, or `intrinsic_alias`"
    )]
    MultipleSelectors,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cross_ref_conflicts_with_other_selector_kinds() {
        let selector: RawMemberSelector = serde_json::from_str(
            r#"{ "binding": { "name": "x" }, "cross_ref": { "references": "A" } }"#,
        )
        .unwrap();
        assert!(
            selector.validate().is_err(),
            "a member must use exactly one selector kind",
        );
    }
}
