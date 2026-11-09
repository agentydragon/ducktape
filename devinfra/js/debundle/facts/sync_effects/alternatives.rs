//! Families of alternative paths through one body, and their join.
//!
//! A fork leaves one leaf per feasible path. Every leaf runs the rest of the
//! body in its own path state; the family is joined once, when every leaf has
//! completed, so the body's caller resumes from one state. The join is
//! conservative: a fact survives only if every leaf holds it, a possibility
//! survives if any leaf has it. A join that cannot resume is an outcome the
//! adapter summarizes, never a panic and never a reason not to run the leaves.

pub(super) mod execution_state;
pub(super) mod outcome;

use super::kernel::context::KernelContext;
use super::kernel::frame::{LocalExecutionAttempt, LocalFrame};
use super::value_model::Context;
use super::world::join::Joined;
use super::world::{Revision, World};

/// One completed path of a body.
pub(super) struct Leaf {
    pub(super) frame: LocalFrame,
    pub(super) attempt: LocalExecutionAttempt,
}

/// The completed paths of one body.
#[must_use]
pub(super) enum Family<B> {
    /// No test forked: the adapter's live state is this leaf's.
    One(Box<Leaf>),
    /// Each leaf with the path state it completed in. The adapter's live
    /// state is unspecified until its caller installs the join.
    Forked(Vec<(Leaf, B)>),
}

/// The state one path owns: whatever a later operation on the same path reads
/// back. Its world is exposed so sibling leaves get disjoint identities.
pub(super) trait PathState {
    fn world(&self) -> &World;
}

/// Identities sibling leaves must not share. `World::join` matches lexical
/// cells by id and invocation memos key on the revision counters, so a cell
/// id or a revision one leaf minted must never name different contents in a
/// sibling.
#[derive(Clone, Copy, Default)]
struct Identities {
    cells: usize,
    revision: Revision,
}

impl Identities {
    fn observe(&mut self, world: &World) {
        self.cells = self.cells.max(world.cells.identities());
        self.revision.prefix = self.revision.prefix.max(world.revision.prefix);
        self.revision.reachability = self.revision.reachability.max(world.revision.reachability);
    }

    /// Move a leaf that starts from the fork point past everything its
    /// earlier siblings minted. The contents are unchanged; only the next
    /// cell id and the revision differ.
    fn separate(self, world: &mut World) {
        world.cells.reserve_identities(self.cells);
        world.revision.prefix = self.revision.prefix;
        world.revision.reachability = self.revision.reachability;
        world.revision.bump_prefix();
        world.revision.bump_reachability();
    }
}

/// Run every child from the adapter's current state. The first child keeps
/// the fork point's identities, so it shares what was memoized before the
/// fork; each later child starts past its earlier siblings.
pub(super) fn explore<C: KernelContext, K>(
    context: &mut C,
    children: Vec<K>,
    mut run: impl FnMut(&mut C, K) -> Family<C::Branch>,
) -> Family<C::Branch> {
    let base = context.branch();
    let mut identities = Identities::default();
    let mut leaves = Vec::with_capacity(children.len());
    for (index, child) in children.into_iter().enumerate() {
        if index > 0 {
            context.enter(base.clone());
            identities.separate(context.world_mut());
        }
        match run(context, child) {
            Family::One(leaf) => {
                identities.observe(context.world());
                leaves.push((*leaf, context.branch()));
            }
            Family::Forked(nested) => {
                for (_, branch) in &nested {
                    identities.observe(branch.world());
                }
                leaves.extend(nested);
            }
        }
    }
    Family::Forked(leaves)
}

impl<B> Family<B> {
    /// Whether no leaf was refused: a consumer with no summary to fall back
    /// on can use a family only when this holds.
    pub(super) fn all_completed(&self) -> bool {
        match self {
            Self::One(leaf) => matches!(leaf.attempt, LocalExecutionAttempt::Completed(_)),
            Self::Forked(leaves) => leaves
                .iter()
                .all(|(leaf, _)| matches!(leaf.attempt, LocalExecutionAttempt::Completed(_))),
        }
    }
}

/// The world every leaf of a family may have left. A refusal still carries
/// the pointwise join, marked incomplete, for the caller's remainder summary.
pub(super) fn join_worlds<'a>(worlds: impl IntoIterator<Item = &'a World>) -> Joined {
    World::join(&worlds.into_iter().collect::<Vec<_>>())
}

/// What the leaves' activations may hold between them, for one remainder
/// summary: every value a binding may have, every input and fresh record.
/// It carries no ledger; the summary's caller discards coverage.
pub(super) fn join_frames(frames: Vec<LocalFrame>) -> LocalFrame {
    let mut environment = Context::new();
    for frame in &frames {
        for (binding, values) in &frame.environment {
            environment
                .entry(binding.clone())
                .or_default()
                .extend(values.iter().cloned());
        }
    }
    let mut joined = LocalFrame::new(environment);
    for frame in frames {
        joined.inputs.extend(frame.inputs);
        joined.borrowed_records.extend(frame.borrowed_records);
        joined.fresh_records.extend(frame.fresh_records);
    }
    joined
}
