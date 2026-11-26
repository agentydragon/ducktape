//! Scope-aware alpha bindings and the diagnostic failure trace threaded through matching.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::rc::Rc;

use chunk_facts::{NodeId, NodeKind};

use crate::{Mode, TraceCategory, TraceFailure, Unsupported};

#[derive(Default)]
pub(crate) struct FailureTrace {
    pub(crate) best: Option<TraceFailure>,
    steps: usize,
    pub(crate) limited: bool,
}

impl FailureTrace {
    fn record(&mut self, failure: TraceFailure) {
        if self.best.as_ref().is_none_or(|best| {
            (failure.needle_statement_index, failure.progress)
                > (best.needle_statement_index, best.progress)
        }) {
            self.best = Some(failure);
        }
    }
}

const DIAGNOSTIC_STEP_LIMIT: usize = 50_000;
pub(crate) const DIAGNOSTIC_DEPTH_LIMIT: usize = 256;
pub(crate) const DIAGNOSTIC_LIMIT_REASON: &str = "diagnostic comparison budget reached";

/// Which JS scope an [`AlphaScope`] frame models.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum FrameKind {
    /// The template root or a function/arrow/constructor/setter body: the scope
    /// `var` declarations hoist to.
    Function,
    /// A `catch` clause's param + body.
    Catch,
    /// A block, loop head, `switch` body, or the name of a named function/class
    /// expression: holds `let`/`const`/`class` bindings only.
    Lexical,
}

/// One lexical frame's bijective needle↔subject identifier map.
#[derive(Clone)]
struct AlphaScope {
    kind: FrameKind,
    forward: HashMap<String, String>,
    backward: HashMap<String, String>,
}

impl AlphaScope {
    fn new(kind: FrameKind) -> Self {
        Self {
            kind,
            forward: HashMap::new(),
            backward: HashMap::new(),
        }
    }
}

/// Scope-aware bijective needle↔subject identifier binding accumulated during
/// alpha matching — a stack of lexical frames mirroring JS scoping, so
/// same-spelled locals in sibling scopes (a param reused across two functions, a
/// `const` reused across two blocks) stay independent. References (`match_ref`)
/// resolve against the visible stack innermost-out; bindings (`match_binding`)
/// consult only their own frame, so a binding may shadow an outer one. A `var`
/// binding lands in the innermost [`FrameKind::Function`] frame, as JS hoists it.
/// A template's **free** identifiers (referenced, never declared in the
/// template) bind like any other reference; what each bound to is recorded
/// alongside the frames, for [`Bindings::free_bindings`].
/// Cloneable so run-hole placement can snapshot/restore across backtracking.
#[derive(Clone)]
pub(crate) struct Bindings<'f> {
    scopes: Vec<AlphaScope>,
    /// Matching the declarators of a `var` declaration: its bindings hoist.
    pub(crate) in_var_decl: bool,
    /// The template's free identifiers (`source_match::free_identifiers`).
    free: &'f BTreeSet<String>,
    /// Each free name bound so far → its subject name, or `None` once it bound
    /// two different ones (in two frames, e.g. sibling function bodies). Not a
    /// root-frame binding: <docs/selector_resolution.md> § "Rejected: binding
    /// free template names in the root frame".
    free_seen: BTreeMap<String, Option<String>>,
    /// Present only for an explicitly requested explanation. Clones share the
    /// recorder while retaining independent alpha state for backtracking.
    pub(crate) trace: Option<Rc<RefCell<FailureTrace>>>,
    pub(crate) statement_index: usize,
    pub(crate) subject_statement_index: usize,
    progress: usize,
}

impl<'f> Bindings<'f> {
    pub(crate) fn new(free: &'f BTreeSet<String>) -> Self {
        Self {
            scopes: vec![AlphaScope::new(FrameKind::Function)],
            in_var_decl: false,
            free,
            free_seen: BTreeMap::new(),
            trace: None,
            statement_index: 0,
            subject_statement_index: 0,
            progress: 0,
        }
    }

    pub(crate) fn with_trace(free: &'f BTreeSet<String>, trace: Rc<RefCell<FailureTrace>>) -> Self {
        let mut bindings = Self::new(free);
        bindings.trace = Some(trace);
        bindings
    }

    pub(crate) fn fail(
        &self,
        needle_node: NodeId,
        subject_node: NodeId,
        category: TraceCategory,
        reason: impl FnOnce() -> String,
    ) {
        if let Some(trace) = &self.trace {
            trace.borrow_mut().record(TraceFailure {
                needle_statement_index: self.statement_index,
                subject_statement_index: self.subject_statement_index,
                needle_node,
                subject_node,
                category,
                reason: reason(),
                progress: self.progress,
            });
        }
    }

    pub(crate) fn step(&mut self) -> Result<(), Unsupported> {
        if let Some(trace) = &self.trace {
            let mut trace = trace.borrow_mut();
            if trace.steps >= DIAGNOSTIC_STEP_LIMIT {
                trace.limited = true;
                return Err(Unsupported {
                    reason: DIAGNOSTIC_LIMIT_REASON,
                });
            }
            trace.steps += 1;
            self.progress = self.progress.saturating_add(1);
        }
        Ok(())
    }

    pub(crate) fn expected_ident(&self, needle: &str) -> Option<&str> {
        self.scopes
            .iter()
            .rev()
            .find_map(|scope| scope.forward.get(needle).map(String::as_str))
    }

    pub(crate) fn push_scope(&mut self, kind: FrameKind) {
        self.scopes.push(AlphaScope::new(kind));
    }

    pub(crate) fn pop_scope(&mut self) {
        self.scopes.pop();
    }

    fn innermost(&self, pick: impl Fn(FrameKind) -> bool) -> usize {
        self.scopes
            .iter()
            .rposition(|scope| pick(scope.kind))
            .expect("the root frame is a function frame")
    }

    /// Match an identifier **reference**: consult the visible scope stack
    /// (innermost-out), then resolve the unbound case per `mode`. A prebound
    /// mapping is honored in **either** mode — that is how a `target_binding`
    /// prebind forces one needle name onto one subject name even under `Exact`
    /// (where the unbound fallback is exact spelling, not a fresh alpha pair).
    /// An unbound reference names something outside the template; it binds in
    /// the innermost non-lexical frame.
    pub(crate) fn match_ref(&mut self, needle: &str, subject: &str, mode: Mode) -> bool {
        for scope in self.scopes.iter().rev() {
            if let Some(mapped) = scope.forward.get(needle) {
                return mapped == subject;
            }
            if scope.backward.contains_key(subject) {
                return false;
            }
        }
        let frame = self.innermost(|kind| kind != FrameKind::Lexical);
        let bound = self.resolve_unbound(frame, needle, subject, mode);
        if bound {
            self.record_free(needle, subject);
        }
        bound
    }

    fn record_free(&mut self, needle: &str, subject: &str) {
        if !self.free.contains(needle) {
            return;
        }
        match self.free_seen.get_mut(needle) {
            Some(seen) => {
                if seen.as_deref() != Some(subject) {
                    *seen = None;
                }
            }
            None => {
                self.free_seen
                    .insert(needle.to_string(), Some(subject.to_string()));
            }
        }
    }

    /// Match an identifier **binding** (declaration): consult only the frame it
    /// declares into, so it may shadow an outer binding of the same spelling.
    pub(crate) fn match_binding(&mut self, needle: &str, subject: &str, mode: Mode) -> bool {
        let frame = if self.in_var_decl {
            self.innermost(|kind| kind == FrameKind::Function)
        } else {
            self.scopes.len() - 1
        };
        let scope = &self.scopes[frame];
        if let Some(mapped) = scope.forward.get(needle) {
            return mapped == subject;
        }
        if scope.backward.contains_key(subject) {
            return false;
        }
        self.resolve_unbound(frame, needle, subject, mode)
    }

    /// Resolve a needle↔subject pair that neither side has mapped yet: under
    /// `AlphaAll` bind them as a fresh alpha pair in `frame`; under `Exact`
    /// require identical spellings (no new binding). The pre-binding gate
    /// (`match_ref`/`match_binding`) already short-circuited a known mapping, so
    /// this only sees genuinely-free names.
    fn resolve_unbound(&mut self, frame: usize, needle: &str, subject: &str, mode: Mode) -> bool {
        match mode {
            Mode::Exact => needle == subject,
            Mode::AlphaAll => {
                self.bind(frame, needle, subject);
                true
            }
        }
    }

    fn bind(&mut self, frame: usize, needle: &str, subject: &str) {
        let scope = &mut self.scopes[frame];
        scope
            .forward
            .insert(needle.to_string(), subject.to_string());
        scope
            .backward
            .insert(subject.to_string(), needle.to_string());
    }

    /// Force needle↔subject mappings in the root frame before matching (the
    /// `target_binding` alpha coupling). Honors an existing mapping; fails if
    /// either side is already mapped incompatibly. Used in both identifier modes.
    pub(crate) fn prebind(&mut self, pairs: &[(&str, &str)]) -> bool {
        debug_assert_eq!(self.scopes.len(), 1, "prebinding precedes any push");
        pairs.iter().all(|&(needle, subject)| {
            let root = &self.scopes[0];
            match (root.forward.get(needle), root.backward.get(subject)) {
                (Some(mapped), _) => mapped == subject,
                (None, Some(_)) => false,
                (None, None) => {
                    self.bind(0, needle, subject);
                    self.record_free(needle, subject);
                    true
                }
            }
        })
    }

    /// What each free identifier bound to in this match, for the free names that
    /// bound one subject name throughout.
    pub(crate) fn free_bindings(&self) -> BTreeMap<String, String> {
        self.free_seen
            .iter()
            .filter_map(|(name, subject)| Some((name.clone(), subject.clone()?)))
            .collect()
    }
}

/// The alpha frame a node opens around its children, if any.
pub(crate) fn alpha_frame(kind: NodeKind) -> Option<FrameKind> {
    match kind {
        NodeKind::Function
        | NodeKind::AsyncFunction
        | NodeKind::GeneratorFunction
        | NodeKind::AsyncGeneratorFunction
        | NodeKind::Arrow
        | NodeKind::AsyncArrow
        | NodeKind::Constructor
        | NodeKind::Setter => Some(FrameKind::Function),
        NodeKind::Catch => Some(FrameKind::Catch),
        NodeKind::Block
        | NodeKind::For
        | NodeKind::ForIn
        | NodeKind::ForOf
        | NodeKind::Switch
        | NodeKind::FnExpr
        | NodeKind::ClassExpr => Some(FrameKind::Lexical),
        _ => None,
    }
}
