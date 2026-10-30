//! Loader-closed fact behind the export-reentry policy (SPEC.md § Host
//! behavior).
//!
//! A target chunk is loader-closed when the declared host loader cannot hand
//! any code the target's namespace while the target evaluates. Every host
//! loader site in every analyzed chunk counts as reachable during that
//! evaluation: a callback any chunk published earlier can run then, so the
//! chunk holding a site does not bound when it executes. Each site must be a
//! literal request for a declared reentry-free builtin or for an analyzed chunk
//! whose static and loader closure excludes the target and loads no unproved
//! code.
//!
//! `Reach` grows to each chunk's static and loader closure. `LoaderClosed`
//! failures grow with it, so a target is closed only in the completed closure;
//! no rule consumes an intermediate verdict.

use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;
use spec::{
    HostBehavior, LoaderResultForEvaluatingModule, NodeBuiltinModule,
    SynchronousLoadRunsAppCallbacks,
};

use crate::analysis_inputs::{HostBehaviorKey, ProofDependencies};
use crate::ids::ChunkId;
use crate::inference::{FactContext, IterativeInference, IterativeRules};

/// Why a target is not loader-closed. A report keeps the first witness and the
/// occurrence count per kind.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LoaderClosureFailureKind {
    /// A loader key the derivation reads is omitted or `unknown`.
    UndeclaredHostBehavior,
    /// A loader value or call whose target the source does not fix: non-literal
    /// or aliased `import.meta.require`, global `require`, `module`,
    /// `import.meta` as a value, dynamic `import()`.
    OpaqueLoaderSite,
    /// A request for the `module` builtin, whose `createRequire` makes loaders.
    LoaderFactoryRequest,
    /// A literal loader request that names no analyzed chunk entry and no
    /// recognized builtin.
    UnresolvableLoaderRequest,
    /// A literal loader request for a builtin absent from
    /// `reentry_free_builtin_modules`.
    NonReentryFreeBuiltinRequest,
    /// A literal loader request whose loaded closure contains the target.
    LoaderRequestReachesTarget,
    /// A static request in a loaded closure that is not a resolved ordinary
    /// request for an analyzed chunk entry or a declared reentry-free builtin.
    LoadedClosureRequest,
    LoadedClosureTopLevelAwait,
    /// A loaded closure contains a vendor-replaced chunk, whose code is unseen.
    LoadedClosureVendorChunk,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd)]
pub enum LoaderRequestTarget {
    ReentryFreeBuiltin(NodeBuiltinModule),
    Chunk(ChunkId),
}

#[derive(Debug, Clone)]
pub struct LoaderRequest<W> {
    pub target: LoaderRequestTarget,
    pub witness: W,
}

/// One chunk's loader-relevant source facts. `W` is the caller's site witness;
/// its order picks the reported first witness.
#[derive(Debug, Clone)]
pub struct ChunkLoaderSurface<W> {
    /// Chunks this chunk's static requests resolve to.
    pub static_targets: BTreeSet<ChunkId>,
    /// Literal loader requests resolved to a declared reentry-free builtin or
    /// an analyzed chunk entry.
    pub requests: Vec<LoaderRequest<W>>,
    /// Sites that keep every target open wherever they occur.
    pub site_failures: Vec<(LoaderClosureFailureKind, W)>,
    /// Facts that keep a target open only when a loader request evaluates
    /// this chunk.
    pub load_failures: Vec<(LoaderClosureFailureKind, W)>,
}

impl<W> ChunkLoaderSurface<W> {
    pub fn has_loader_sites(&self) -> bool {
        !self.requests.is_empty() || !self.site_failures.is_empty()
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LoaderClosureWitness<W> {
    HostBehavior(HostBehaviorKey),
    Site(W),
}

#[derive(Debug, Clone, Serialize)]
pub struct LoaderClosureFailure<W> {
    pub occurrences: usize,
    pub first: LoaderClosureWitness<W>,
}

/// The completed loader-closed judgment for one target.
#[derive(Debug, Clone)]
pub struct LoaderClosure<W> {
    pub failures: BTreeMap<LoaderClosureFailureKind, LoaderClosureFailure<W>>,
    /// Chunks a loader request can evaluate, with their static and loader
    /// closures.
    pub loaded_chunks: BTreeSet<ChunkId>,
    /// Host keys the certificate consumed; empty unless closed.
    pub dependencies: ProofDependencies,
}

impl<W> LoaderClosure<W> {
    pub fn is_closed(&self) -> bool {
        self.failures.is_empty()
    }
}

/// Solve the loader-closed fact for each target over every analyzed chunk.
pub fn solve_loader_closure<W: Clone + Ord>(
    chunks: &BTreeMap<ChunkId, ChunkLoaderSurface<W>>,
    host_behavior: &HostBehavior,
    targets: &BTreeSet<ChunkId>,
) -> BTreeMap<ChunkId, LoaderClosure<W>> {
    let rules = LoaderClosureRules {
        chunks,
        host_behavior,
    };
    let producers = chunks
        .keys()
        .map(|chunk| Rule::Reach(*chunk))
        .chain(targets.iter().map(|target| Rule::LoaderClosed(*target)));
    let facts = IterativeInference::new(rules).solve_values_resumable(producers);
    targets
        .iter()
        .map(|target| {
            let judgment = facts
                .get(&Key::LoaderClosed(*target))
                .expect("every target rule proposes its verdict");
            let Fact::LoaderClosed(verdict) = judgment.value() else {
                unreachable!("loader-closed key holds a verdict");
            };
            (
                *target,
                LoaderClosure {
                    failures: verdict.failures.clone(),
                    loaded_chunks: verdict.loaded_chunks.clone(),
                    dependencies: judgment.dependencies().clone(),
                },
            )
        })
        .collect()
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd)]
enum Key {
    Reach(ChunkId),
    LoaderClosed(ChunkId),
}

/// Reach rules order before target rules, so a round settles reachability
/// before evaluating the verdicts that read it.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd)]
enum Rule {
    Reach(ChunkId),
    LoaderClosed(ChunkId),
}

enum Fact<W> {
    Reach(BTreeSet<ChunkId>),
    LoaderClosed(Verdict<W>),
}

struct Verdict<W> {
    failures: BTreeMap<LoaderClosureFailureKind, LoaderClosureFailure<W>>,
    loaded_chunks: BTreeSet<ChunkId>,
}

impl<W: Ord> Verdict<W> {
    fn new() -> Self {
        Self {
            failures: BTreeMap::new(),
            loaded_chunks: BTreeSet::new(),
        }
    }

    fn add(&mut self, kind: LoaderClosureFailureKind, witness: LoaderClosureWitness<W>) {
        self.join_failure(
            kind,
            LoaderClosureFailure {
                occurrences: 1,
                first: witness,
            },
            |current, added| current + added,
        );
    }

    /// `combine` sums occurrences within one evaluation and takes the larger
    /// count across evaluations, which recount every site.
    fn join_failure(
        &mut self,
        kind: LoaderClosureFailureKind,
        failure: LoaderClosureFailure<W>,
        combine: impl Fn(usize, usize) -> usize,
    ) -> bool {
        use std::collections::btree_map::Entry;
        match self.failures.entry(kind) {
            Entry::Vacant(entry) => {
                entry.insert(failure);
                true
            }
            Entry::Occupied(mut entry) => {
                let current = entry.get_mut();
                let occurrences = combine(current.occurrences, failure.occurrences);
                let mut changed = occurrences != current.occurrences;
                current.occurrences = occurrences;
                if failure.first < current.first {
                    current.first = failure.first;
                    changed = true;
                }
                changed
            }
        }
    }
}

struct LoaderClosureRules<'a, W> {
    chunks: &'a BTreeMap<ChunkId, ChunkLoaderSurface<W>>,
    host_behavior: &'a HostBehavior,
}

impl<W: Clone + Ord> LoaderClosureRules<'_, W> {
    fn evaluate_reach(&self, chunk: ChunkId, context: &mut impl FactContext<Key, Fact<W>>) {
        let Some(surface) = self.chunks.get(&chunk) else {
            return;
        };
        let direct: BTreeSet<_> = surface
            .static_targets
            .iter()
            .copied()
            .chain(
                surface
                    .requests
                    .iter()
                    .filter_map(|request| match request.target {
                        LoaderRequestTarget::Chunk(target) => Some(target),
                        LoaderRequestTarget::ReentryFreeBuiltin(_) => None,
                    }),
            )
            .collect();
        let mut reach = direct.clone();
        for next in direct {
            if let Some(Fact::Reach(transitive)) = context.read(Key::Reach(next)) {
                reach.extend(transitive.iter().copied());
            }
        }
        context.propose(Key::Reach(chunk), Fact::Reach(reach));
    }

    fn evaluate_loader_closed(
        &self,
        target: ChunkId,
        context: &mut impl FactContext<Key, Fact<W>>,
    ) {
        let mut verdict = Verdict::new();
        let mut dependencies = ProofDependencies::default();
        // A loader that runs application callbacks could redirect or observe a
        // request; an unknown evaluating-module result leaves the loader's
        // reentry behavior unconstrained.
        match self.host_behavior.synchronous_load_runs_app_callbacks {
            Some(SynchronousLoadRunsAppCallbacks::Never) => {
                dependencies
                    .host_behavior
                    .insert(HostBehaviorKey::SynchronousLoadRunsAppCallbacks);
            }
            Some(SynchronousLoadRunsAppCallbacks::Unknown) | None => verdict.add(
                LoaderClosureFailureKind::UndeclaredHostBehavior,
                LoaderClosureWitness::HostBehavior(
                    HostBehaviorKey::SynchronousLoadRunsAppCallbacks,
                ),
            ),
        }
        match self.host_behavior.loader_result_for_evaluating_module {
            Some(
                LoaderResultForEvaluatingModule::Throws
                | LoaderResultForEvaluatingModule::ReturnsPartialNamespace,
            ) => {
                dependencies
                    .host_behavior
                    .insert(HostBehaviorKey::LoaderResultForEvaluatingModule);
            }
            Some(LoaderResultForEvaluatingModule::Unknown) | None => verdict.add(
                LoaderClosureFailureKind::UndeclaredHostBehavior,
                LoaderClosureWitness::HostBehavior(
                    HostBehaviorKey::LoaderResultForEvaluatingModule,
                ),
            ),
        }
        for surface in self.chunks.values() {
            for (kind, witness) in &surface.site_failures {
                verdict.add(*kind, LoaderClosureWitness::Site(witness.clone()));
            }
            for request in &surface.requests {
                let loaded = match request.target {
                    LoaderRequestTarget::ReentryFreeBuiltin(builtin) => {
                        dependencies
                            .host_behavior
                            .insert(HostBehaviorKey::ReentryFreeBuiltinModule(builtin));
                        continue;
                    }
                    LoaderRequestTarget::Chunk(loaded) => loaded,
                };
                let mut closure = BTreeSet::from([loaded]);
                if let Some(Fact::Reach(reach)) = context.read(Key::Reach(loaded)) {
                    closure.extend(reach.iter().copied());
                }
                if closure.contains(&target) {
                    verdict.add(
                        LoaderClosureFailureKind::LoaderRequestReachesTarget,
                        LoaderClosureWitness::Site(request.witness.clone()),
                    );
                }
                verdict.loaded_chunks.extend(closure);
            }
        }
        for chunk in &verdict.loaded_chunks.clone() {
            if let Some(surface) = self.chunks.get(chunk) {
                for (kind, witness) in &surface.load_failures {
                    verdict.add(*kind, LoaderClosureWitness::Site(witness.clone()));
                }
            }
        }
        if verdict.failures.is_empty() {
            context.assume(&dependencies);
        }
        context.propose(Key::LoaderClosed(target), Fact::LoaderClosed(verdict));
    }
}

impl<W: Clone + Ord> IterativeRules for LoaderClosureRules<'_, W> {
    type Key = Key;
    type Fact = Fact<W>;
    type Rule = Rule;

    fn evaluate(&self, rule: &Rule, context: &mut impl FactContext<Key, Fact<W>>) {
        match *rule {
            Rule::Reach(chunk) => self.evaluate_reach(chunk, context),
            Rule::LoaderClosed(target) => self.evaluate_loader_closed(target, context),
        }
    }

    fn merge(_key: &Key, current: &mut Fact<W>, contribution: Fact<W>) -> bool {
        match (current, contribution) {
            (Fact::Reach(current), Fact::Reach(contribution)) => {
                let before = current.len();
                current.extend(contribution);
                current.len() != before
            }
            (Fact::LoaderClosed(current), Fact::LoaderClosed(contribution)) => {
                let before = current.loaded_chunks.len();
                current.loaded_chunks.extend(contribution.loaded_chunks);
                let mut changed = current.loaded_chunks.len() != before;
                for (kind, failure) in contribution.failures {
                    changed |= current.join_failure(kind, failure, usize::max);
                }
                changed
            }
            _ => unreachable!("each key holds one fact family"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn surface(
        static_targets: &[usize],
        requests: &[(LoaderRequestTarget, &'static str)],
    ) -> ChunkLoaderSurface<&'static str> {
        ChunkLoaderSurface {
            static_targets: static_targets.iter().map(|id| ChunkId(*id)).collect(),
            requests: requests
                .iter()
                .map(|(target, witness)| LoaderRequest {
                    target: *target,
                    witness: *witness,
                })
                .collect(),
            site_failures: Vec::new(),
            load_failures: Vec::new(),
        }
    }

    fn audited_host() -> HostBehavior {
        HostBehavior {
            loader_result_for_evaluating_module: Some(LoaderResultForEvaluatingModule::Throws),
            synchronous_load_runs_app_callbacks: Some(SynchronousLoadRunsAppCallbacks::Never),
            reentry_free_builtin_modules: BTreeSet::from([NodeBuiltinModule::Path]),
            ..HostBehavior::default()
        }
    }

    fn solve_one(
        chunks: &BTreeMap<ChunkId, ChunkLoaderSurface<&'static str>>,
        host: &HostBehavior,
        target: usize,
    ) -> LoaderClosure<&'static str> {
        solve_loader_closure(chunks, host, &BTreeSet::from([ChunkId(target)]))
            .remove(&ChunkId(target))
            .unwrap()
    }

    #[test]
    fn transitive_loader_and_static_closure_back_to_the_target_is_open() {
        // 0 loads 1; 1 statically imports 2; 2 loads 0.
        let chunks = BTreeMap::from([
            (
                ChunkId(0),
                surface(&[], &[(LoaderRequestTarget::Chunk(ChunkId(1)), "0->1")]),
            ),
            (ChunkId(1), surface(&[2], &[])),
            (
                ChunkId(2),
                surface(&[], &[(LoaderRequestTarget::Chunk(ChunkId(0)), "2->0")]),
            ),
        ]);
        let closure = solve_one(&chunks, &audited_host(), 0);
        assert_eq!(
            closure.failures[&LoaderClosureFailureKind::LoaderRequestReachesTarget].first,
            LoaderClosureWitness::Site("0->1"),
        );
        assert_eq!(
            closure.failures[&LoaderClosureFailureKind::LoaderRequestReachesTarget].occurrences,
            2,
        );
        assert!(closure.dependencies.is_empty());
    }

    #[test]
    fn closed_target_records_the_consumed_host_keys() {
        let chunks = BTreeMap::from([
            (
                ChunkId(0),
                surface(
                    &[1],
                    &[
                        (LoaderRequestTarget::Chunk(ChunkId(2)), "0->2"),
                        (
                            LoaderRequestTarget::ReentryFreeBuiltin(NodeBuiltinModule::Path),
                            "0->path",
                        ),
                    ],
                ),
            ),
            (ChunkId(1), surface(&[], &[])),
            (ChunkId(2), surface(&[1], &[])),
        ]);
        let closure = solve_one(&chunks, &audited_host(), 0);
        assert!(closure.is_closed(), "{:?}", closure.failures);
        assert_eq!(
            closure.loaded_chunks,
            BTreeSet::from([ChunkId(1), ChunkId(2)])
        );
        assert_eq!(
            closure.dependencies.host_behavior,
            BTreeSet::from([
                HostBehaviorKey::LoaderResultForEvaluatingModule,
                HostBehaviorKey::SynchronousLoadRunsAppCallbacks,
                HostBehaviorKey::ReentryFreeBuiltinModule(NodeBuiltinModule::Path),
            ])
        );
    }

    #[test]
    fn load_failures_count_only_inside_a_loaded_closure() {
        let mut unloaded = surface(&[], &[]);
        unloaded.load_failures.push((
            LoaderClosureFailureKind::LoadedClosureTopLevelAwait,
            "unloaded",
        ));
        let mut loaded = surface(&[], &[]);
        loaded
            .load_failures
            .push((LoaderClosureFailureKind::LoadedClosureRequest, "loaded"));
        let chunks = BTreeMap::from([
            (
                ChunkId(0),
                surface(&[], &[(LoaderRequestTarget::Chunk(ChunkId(1)), "0->1")]),
            ),
            (ChunkId(1), loaded),
            (ChunkId(2), unloaded),
        ]);
        let closure = solve_one(&chunks, &audited_host(), 0);
        assert_eq!(
            closure.failures.keys().copied().collect::<Vec<_>>(),
            [LoaderClosureFailureKind::LoadedClosureRequest]
        );
    }

    #[test]
    fn omitted_and_unknown_loader_keys_keep_the_target_open() {
        let chunks = BTreeMap::from([(ChunkId(0), surface(&[], &[]))]);
        for host in [
            HostBehavior::default(),
            HostBehavior {
                loader_result_for_evaluating_module: Some(LoaderResultForEvaluatingModule::Unknown),
                synchronous_load_runs_app_callbacks: Some(SynchronousLoadRunsAppCallbacks::Unknown),
                ..HostBehavior::default()
            },
        ] {
            let closure = solve_one(&chunks, &host, 0);
            assert_eq!(
                closure.failures[&LoaderClosureFailureKind::UndeclaredHostBehavior].occurrences,
                2
            );
        }
    }
}
