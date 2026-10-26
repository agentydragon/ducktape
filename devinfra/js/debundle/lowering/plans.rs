//! Output plan structures kept by lowering. Spec-derived request
//! construction lives in `spec_resolution`.

use analysis::ModuleRole;
use source_identity::StatementId;

use std::collections::{BTreeMap, BTreeSet, HashMap};

#[derive(Debug, Clone)]
pub(super) struct ModulePlan {
    pub(super) id: String,
    pub(super) target_file: String,
    /// Logical module path the spec asked for (e.g. `"ai/mcp/foo"`).
    /// Distinct from `target_file`, which is the chunk-relative
    /// emitted file path (e.g. `"modules/foo.js"`).
    pub(super) target_path: String,
    /// Assignment policy for this destination. Explicit module requests are
    /// `Specified`; outputs used for unassigned owners are `Unassigned`,
    /// including the catch-all and synthesized atomic-group outputs. This
    /// role does not identify the physical DFS root.
    pub(super) role: ModuleRole,
    /// Local-name → public-export-name for every owned binding this
    /// plan claims (i.e. members whose `selector.binding.kind` is
    /// _not_ `ImportSpecifier`). ImportSpecifier-bound members live
    /// in `AssignmentAnalysis.bindings` as `BindingKind::Imported` and their
    /// emit is driven from there. Iteration order is undefined;
    /// emit / report sites sort by local name before consuming so
    /// the emitted source and JSON shapes stay deterministic.
    pub(super) bindings: HashMap<String, String>,
    /// Source bindings whose public names came explicitly from the spec,
    /// including names equal to the original binding spelling.
    pub(super) spec_named_bindings: BTreeSet<String>,
    /// Original source statements of anonymous-statement members
    /// claimed by this module. These source units have empty
    /// `declared_bindings`, so they can't be addressed by name —
    /// the spec resolves them by AST shape (see
    /// [`spec::LogicalModule::anonymous_statements`]). The
    /// materializer routes each such statement into this module's
    /// body in source order, alongside the named members.
    pub(super) anonymous_statements: Vec<StatementId>,
    /// Source-chunk body index → anonymous-statement comment text.
    /// Emitted as a `// ...` block immediately above the matched
    /// statement in the generated module body.
    pub(super) anonymous_statement_comments: BTreeMap<StatementId, String>,
    /// Module-level human-readable comment from the spec, if any.
    /// Emitted at the top of the generated module file, before
    /// imports. See [`spec::LogicalModule::comment`].
    pub(super) comment: Option<String>,
    /// Local-name → per-member comment text from the spec, for the
    /// bindings this plan claims. Emitted as a `// ...` block above
    /// the binding's source unit statement in the generated module body.
    /// See [`spec::Member::comment`].
    pub(super) binding_comments: BTreeMap<String, String>,
    /// Local-name → short spec-facing origin for each owned binding claim.
    /// This is diagnostic metadata; lowering behavior is driven by
    /// [`ModulePlan::bindings`].
    pub(super) binding_claim_origins: BTreeMap<String, String>,
}
