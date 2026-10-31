//! Loader-closed fact behind the export-reentry policy (SPEC.md § Host
//! behavior).
//!
//! A target chunk is loader-closed when the declared host loader cannot hand
//! any code the target's namespace while the target evaluates. A site counts
//! when it can run in that window. A site in top-level code runs only while
//! its own module's body does, which is inside the window only for the target,
//! its static closure and chunks a counted request loads. A site in a function
//! can run whenever a callback runs, except in a chunk that statically imports
//! the target outside a cycle with it: that chunk's body, and so every
//! reference to its functions, starts only after the target finishes, unless a
//! counted request loads it. Each counted site must be a literal request for a
//! declared reentry-free module or for an analyzed chunk whose static and
//! loader closure excludes the target and loads no unproved code.
//!
//! `Reach` grows to each chunk's static and loader closure. `LoaderClosed`
//! failures grow with it, so a target is closed only in the completed closure;
//! no rule consumes an intermediate verdict.

use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;
use spec::{
    EmbeddedLoader, HostBehavior, HostModule, LoaderResultForEvaluatingModule,
    SynchronousLoadRunsAppCallbacks,
};

use crate::analysis_inputs::{HostBehaviorKey, ProgramPremiseKey, ProofDependencies};
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
    /// `import.meta` as a value.
    OpaqueLoaderSite,
    /// A request for the `module` builtin, whose `createRequire` makes loaders.
    LoaderFactoryRequest,
    /// A literal loader request that names no analyzed chunk entry and no
    /// recognized builtin.
    UnresolvableLoaderRequest,
    /// A literal loader request for a host module absent from
    /// `reentry_free_builtin_modules` and `reentry_free_bun_modules`.
    NonReentryFreeBuiltinRequest,
    /// A literal loader request for an embedded file whose host loader is
    /// absent from `reentry_free_embedded_loaders`.
    NonReentryFreeEmbeddedFile,
    /// A literal loader request whose loaded closure contains the target.
    LoaderRequestReachesTarget,
    /// A static request in a loaded closure that is not a resolved ordinary
    /// request for an analyzed chunk entry or a declared reentry-free builtin.
    LoadedClosureRequest,
    LoadedClosureTopLevelAwait,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd)]
pub enum LoaderRequestTarget {
    ReentryFreeBuiltin(HostModule),
    EmbeddedFile(EmbeddedLoader),
    Chunk(ChunkId),
}

/// Where a site can run relative to its module's evaluation.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum SitePlacement {
    /// A direct load in top-level code: it runs only while its module's body
    /// evaluates.
    ModuleBody,
    /// A direct load in the code of this [`ChunkCallables`] node: it runs only
    /// when that node is entered.
    InFunction(u32),
    /// Anything a callback can reach.
    Callable,
}

/// How one chunk's code can be entered, with imports resolved to chunks.
/// Node 0 is top-level code.
#[derive(Debug, Clone, Default)]
pub struct ChunkCallables {
    pub nodes: Vec<CallableFacts>,
    /// Local export name to the function node it publishes.
    pub exports: BTreeMap<String, u32>,
    /// `export { imported as exported } from` with the source resolved.
    pub reexports: BTreeMap<String, (ChunkId, String)>,
    pub star_sources: Vec<ChunkId>,
    /// Imported functions used as values: anyone may call them later.
    pub escaped_imports: Vec<(ChunkId, String)>,
    /// Chunks whose whole namespace this chunk obtains statically.
    pub namespace_sources: Vec<ChunkId>,
}

#[derive(Debug, Clone, Default)]
pub struct CallableFacts {
    /// Its value can reach code the chunk does not name, so any code may call
    /// it once it exists.
    pub pooled: bool,
    pub local_callees: Vec<u32>,
    /// Direct calls of an imported binding, resolved to the defining chunk.
    pub import_calls: Vec<(ChunkId, String)>,
}

#[derive(Debug, Clone)]
pub struct LoaderRequest<W> {
    pub target: LoaderRequestTarget,
    pub witness: W,
    pub placement: SitePlacement,
}

/// One chunk's loader-relevant source facts. `W` is the caller's site witness;
/// its order picks the reported first witness.
#[derive(Debug, Clone)]
pub struct ChunkLoaderSurface<W> {
    /// Chunks this chunk's static requests resolve to.
    pub static_targets: BTreeSet<ChunkId>,
    pub callables: ChunkCallables,
    /// Literal loader requests resolved to a declared reentry-free builtin or
    /// an analyzed chunk entry.
    pub requests: Vec<LoaderRequest<W>>,
    /// Sites that keep a target open when they can run in its window.
    pub site_failures: Vec<(LoaderClosureFailureKind, W, SitePlacement)>,
    /// Facts that keep a target open only when a loader request evaluates
    /// this chunk.
    pub load_failures: Vec<(LoaderClosureFailureKind, W)>,
    /// Mandatorily suspended sites the caller excluded under
    /// `synchronous_load_drains_microtasks: never`: they run in a promise job,
    /// which no synchronous load can start.
    pub suspended_sites: Vec<W>,
    /// Non-literal loader calls the caller excluded under
    /// `program_premises.non_literal_loader_calls: native_addons_only`: each
    /// fails or loads a native addon whose initializer runs no application
    /// code.
    pub premised_sites: Vec<W>,
}

impl<W> ChunkLoaderSurface<W> {
    pub fn fail(&mut self, kind: LoaderClosureFailureKind, witness: W, placement: SitePlacement) {
        self.site_failures.push((kind, witness, placement));
    }

    pub fn has_loader_sites(&self) -> bool {
        !self.requests.is_empty()
            || !self.site_failures.is_empty()
            || !self.suspended_sites.is_empty()
            || !self.premised_sites.is_empty()
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
    /// Mandatorily suspended sites excluded from the closure.
    pub suspended_sites: usize,
    /// Non-literal loader calls excluded under the program premise.
    pub premised_sites: usize,
    /// Requests and failing sites that cannot run in the target's window.
    pub outside_window_sites: usize,
    /// Closures of counted requests that contain the target: the chunks that
    /// can evaluate against the target's live bindings while it evaluates.
    pub reentry_window: BTreeSet<ChunkId>,
    /// Whether a counted request loads the target itself, returning its
    /// namespace.
    pub target_requested: bool,
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
    let mut static_importers = BTreeMap::<ChunkId, BTreeSet<ChunkId>>::new();
    for (chunk, surface) in chunks {
        for imported in &surface.static_targets {
            static_importers
                .entry(*imported)
                .or_default()
                .insert(*chunk);
        }
    }
    let rules = LoaderClosureRules {
        chunks,
        host_behavior,
        static_importers,
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
                    suspended_sites: verdict.suspended_sites,
                    premised_sites: verdict.premised_sites,
                    outside_window_sites: verdict.outside_window_sites,
                    reentry_window: verdict.reentry_window.clone(),
                    target_requested: verdict.target_requested,
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
    suspended_sites: usize,
    premised_sites: usize,
    outside_window_sites: usize,
    reentry_window: BTreeSet<ChunkId>,
    target_requested: bool,
}

impl<W: Ord> Verdict<W> {
    fn new() -> Self {
        Self {
            failures: BTreeMap::new(),
            loaded_chunks: BTreeSet::new(),
            suspended_sites: 0,
            premised_sites: 0,
            outside_window_sites: 0,
            reentry_window: BTreeSet::new(),
            target_requested: false,
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
    static_importers: BTreeMap<ChunkId, BTreeSet<ChunkId>>,
}

/// Chunks reachable from `start` over `edges`, excluding `start` unless a cycle
/// returns to it.
fn closure_over(
    start: ChunkId,
    edges: impl Fn(ChunkId) -> Option<BTreeSet<ChunkId>>,
) -> BTreeSet<ChunkId> {
    let mut seen = BTreeSet::new();
    let mut pending = vec![start];
    while let Some(chunk) = pending.pop() {
        for next in edges(chunk).into_iter().flatten() {
            if seen.insert(next) {
                pending.push(next);
            }
        }
    }
    seen
}

impl<W: Clone + Ord> LoaderClosureRules<'_, W> {
    /// The function nodes an export name of `chunk` can denote, following
    /// re-exports.
    fn export_nodes(
        &self,
        chunk: ChunkId,
        name: &str,
        seen: &mut BTreeSet<(ChunkId, String)>,
        out: &mut Vec<(ChunkId, u32)>,
    ) {
        if !seen.insert((chunk, name.to_string())) {
            return;
        }
        let Some(surface) = self.chunks.get(&chunk) else {
            return;
        };
        let callables = &surface.callables;
        if let Some(&node) = callables.exports.get(name) {
            out.push((chunk, node));
        } else if let Some((source, imported)) = callables.reexports.get(name) {
            self.export_nodes(*source, imported, seen, out);
        } else if name != "default" {
            for source in &callables.star_sources {
                self.export_nodes(*source, name, seen, out);
            }
        }
    }

    /// Function nodes that can run while the target evaluates. The roots are
    /// the top-level code of the chunks whose bodies run in the window, and
    /// every pooled function of every chunk that can have run before the
    /// target finishes: an opaque call anywhere in the window may invoke it.
    /// Direct local and imported calls, imported values that escape, and
    /// namespaces obtained statically extend the set.
    fn window_callables(
        &self,
        body: &BTreeSet<ChunkId>,
        loaded: &BTreeSet<ChunkId>,
        after: &BTreeSet<ChunkId>,
    ) -> std::collections::HashSet<(ChunkId, u32)> {
        let mut reached = std::collections::HashSet::new();
        let mut pending = Vec::new();
        // A chunk whose body cannot start before the target finishes has no
        // function values yet, unless a counted request loads it.
        let running = |chunk: ChunkId| !after.contains(&chunk) || loaded.contains(&chunk);
        let push = |node: (ChunkId, u32),
                    reached: &mut std::collections::HashSet<(ChunkId, u32)>,
                    pending: &mut Vec<(ChunkId, u32)>| {
            if running(node.0) && reached.insert(node) {
                pending.push(node);
            }
        };
        let mut seen = BTreeSet::new();
        let mut exported = Vec::new();
        for (&chunk, surface) in self.chunks {
            if !running(chunk) {
                continue;
            }
            if body.contains(&chunk) || loaded.contains(&chunk) {
                push((chunk, 0), &mut reached, &mut pending);
            }
            let callables = &surface.callables;
            for (index, node) in callables.nodes.iter().enumerate() {
                if node.pooled {
                    push((chunk, index as u32), &mut reached, &mut pending);
                }
            }
            for (source, name) in &callables.escaped_imports {
                self.export_nodes(*source, name, &mut seen, &mut exported);
                seen.clear();
            }
            // A loader request hands its caller the loaded chunk's namespace,
            // whose functions anyone holding it may call.
            let requested = surface
                .requests
                .iter()
                .filter_map(|request| match request.target {
                    LoaderRequestTarget::Chunk(loaded) => Some(loaded),
                    LoaderRequestTarget::ReentryFreeBuiltin(_)
                    | LoaderRequestTarget::EmbeddedFile(_) => None,
                });
            for source in callables.namespace_sources.iter().copied().chain(requested) {
                if let Some(namespace) = self.chunks.get(&source) {
                    let names: Vec<_> = namespace
                        .callables
                        .exports
                        .keys()
                        .chain(namespace.callables.reexports.keys())
                        .cloned()
                        .collect();
                    for name in names {
                        self.export_nodes(source, &name, &mut seen, &mut exported);
                        seen.clear();
                    }
                }
            }
        }
        for node in exported.drain(..) {
            push(node, &mut reached, &mut pending);
        }
        while let Some((chunk, index)) = pending.pop() {
            let Some(node) = self
                .chunks
                .get(&chunk)
                .and_then(|surface| surface.callables.nodes.get(index as usize))
            else {
                continue;
            };
            for &callee in &node.local_callees {
                push((chunk, callee), &mut reached, &mut pending);
            }
            for (source, name) in &node.import_calls {
                self.export_nodes(*source, name, &mut seen, &mut exported);
                seen.clear();
                for target in exported.drain(..) {
                    push(target, &mut reached, &mut pending);
                }
            }
        }
        reached
    }

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
                        LoaderRequestTarget::ReentryFreeBuiltin(_)
                        | LoaderRequestTarget::EmbeddedFile(_) => None,
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

    /// The chunk a request loads, with its static and loader closure.
    fn loaded_closure(
        loaded: ChunkId,
        context: &mut impl FactContext<Key, Fact<W>>,
    ) -> BTreeSet<ChunkId> {
        let mut closure = BTreeSet::from([loaded]);
        if let Some(Fact::Reach(reach)) = context.read(Key::Reach(loaded)) {
            closure.extend(reach.iter().copied());
        }
        closure
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
        let static_closure = closure_over(target, |chunk| {
            self.chunks
                .get(&chunk)
                .map(|surface| surface.static_targets.clone())
        });
        let importers = closure_over(target, |chunk| self.static_importers.get(&chunk).cloned());
        // Chunks whose bodies run in the window under static evaluation, and
        // chunks whose code cannot run before the target finishes.
        let mut body: BTreeSet<ChunkId> = static_closure.iter().copied().collect();
        body.insert(target);
        let after: BTreeSet<ChunkId> = importers.difference(&body).copied().collect();
        // Chunks a counted request evaluates in the window run there whole.
        let mut window_loaded = BTreeSet::new();
        let mut reached = self.window_callables(&body, &window_loaded, &after);
        loop {
            let counted =
                |chunk: &ChunkId,
                 placement: SitePlacement,
                 loaded: &BTreeSet<ChunkId>,
                 reached: &std::collections::HashSet<(ChunkId, u32)>| {
                    match placement {
                        SitePlacement::ModuleBody => body.contains(chunk) || loaded.contains(chunk),
                        SitePlacement::InFunction(node) => reached.contains(&(*chunk, node)),
                        SitePlacement::Callable => loaded.contains(chunk) || !after.contains(chunk),
                    }
                };
            let mut grown = window_loaded.clone();
            for (chunk, surface) in self.chunks {
                for request in &surface.requests {
                    if let LoaderRequestTarget::Chunk(loaded) = request.target
                        && counted(chunk, request.placement, &window_loaded, &reached)
                    {
                        grown.extend(Self::loaded_closure(loaded, context));
                    }
                }
            }
            if grown.len() == window_loaded.len() {
                break;
            }
            window_loaded = grown;
            reached = self.window_callables(&body, &window_loaded, &after);
        }
        let counted = |chunk: &ChunkId, placement: SitePlacement| match placement {
            SitePlacement::ModuleBody => body.contains(chunk) || window_loaded.contains(chunk),
            SitePlacement::InFunction(node) => reached.contains(&(*chunk, node)),
            SitePlacement::Callable => window_loaded.contains(chunk) || !after.contains(chunk),
        };
        for (chunk, surface) in self.chunks {
            verdict.suspended_sites += surface.suspended_sites.len();
            verdict.premised_sites += surface.premised_sites.len();
            for (kind, witness, placement) in &surface.site_failures {
                if counted(chunk, *placement) {
                    verdict.add(*kind, LoaderClosureWitness::Site(witness.clone()));
                } else {
                    verdict.outside_window_sites += 1;
                }
            }
            for request in &surface.requests {
                if !counted(chunk, request.placement) {
                    verdict.outside_window_sites += 1;
                    continue;
                }
                let loaded = match request.target {
                    LoaderRequestTarget::ReentryFreeBuiltin(builtin) => {
                        dependencies
                            .host_behavior
                            .insert(HostBehaviorKey::reentry_free_module(builtin));
                        if matches!(builtin, HostModule::Node(node) if node.reads_process_accessors())
                        {
                            dependencies
                                .program_premises
                                .insert(ProgramPremiseKey::ProcessPrototypeChainAccessors);
                        }
                        continue;
                    }
                    LoaderRequestTarget::EmbeddedFile(loader) => {
                        dependencies
                            .host_behavior
                            .insert(HostBehaviorKey::ReentryFreeEmbeddedLoader(loader));
                        if loader == EmbeddedLoader::Napi {
                            dependencies
                                .program_premises
                                .insert(ProgramPremiseKey::NonLiteralLoaderCalls);
                        }
                        continue;
                    }
                    LoaderRequestTarget::Chunk(loaded) => loaded,
                };
                let closure = Self::loaded_closure(loaded, context);
                if closure.contains(&target) {
                    verdict.target_requested |= loaded == target;
                    verdict.reentry_window.extend(closure.iter().copied());
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
        if verdict.suspended_sites > 0 {
            dependencies
                .host_behavior
                .insert(HostBehaviorKey::SynchronousLoadDrainsMicrotasks);
        }
        if verdict.premised_sites > 0 {
            dependencies
                .program_premises
                .insert(ProgramPremiseKey::NonLiteralLoaderCalls);
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
                if contribution.suspended_sites > current.suspended_sites {
                    current.suspended_sites = contribution.suspended_sites;
                    changed = true;
                }
                if contribution.premised_sites > current.premised_sites {
                    current.premised_sites = contribution.premised_sites;
                    changed = true;
                }
                // A diagnostic count that shrinks as the window grows; the
                // latest evaluation is the most complete.
                current.outside_window_sites = contribution.outside_window_sites;
                let before = current.reentry_window.len();
                current.reentry_window.extend(contribution.reentry_window);
                changed |= current.reentry_window.len() != before;
                if contribution.target_requested && !current.target_requested {
                    current.target_requested = true;
                    changed = true;
                }
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
    use spec::NodeBuiltinModule;

    fn surface(
        static_targets: &[usize],
        requests: &[(LoaderRequestTarget, &'static str)],
    ) -> ChunkLoaderSurface<&'static str> {
        ChunkLoaderSurface {
            static_targets: static_targets.iter().map(|id| ChunkId(*id)).collect(),
            callables: ChunkCallables::default(),
            requests: requests
                .iter()
                .map(|(target, witness)| LoaderRequest {
                    target: *target,
                    witness: *witness,
                    placement: SitePlacement::Callable,
                })
                .collect(),
            site_failures: Vec::new(),
            load_failures: Vec::new(),
            suspended_sites: Vec::new(),
            premised_sites: Vec::new(),
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
                            LoaderRequestTarget::ReentryFreeBuiltin(HostModule::Node(
                                NodeBuiltinModule::Path,
                            )),
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

    fn failing(
        mut surface: ChunkLoaderSurface<&'static str>,
        witness: &'static str,
        placement: SitePlacement,
    ) -> ChunkLoaderSurface<&'static str> {
        surface.fail(
            LoaderClosureFailureKind::OpaqueLoaderSite,
            witness,
            placement,
        );
        surface
    }

    fn failure_witnesses(closure: &LoaderClosure<&'static str>) -> Option<usize> {
        closure
            .failures
            .get(&LoaderClosureFailureKind::OpaqueLoaderSite)
            .map(|failure| failure.occurrences)
    }

    /// 1 imports the target 0; 2 is unrelated; 0 imports 3.
    #[test]
    fn a_site_counts_only_when_it_can_run_in_the_target_window() {
        let solve = |site_chunk: usize, placement: SitePlacement| {
            let mut chunks = BTreeMap::from([
                (ChunkId(0), surface(&[3], &[])),
                (ChunkId(1), surface(&[0], &[])),
                (ChunkId(2), surface(&[], &[])),
                (ChunkId(3), surface(&[], &[])),
            ]);
            let chunk = chunks.remove(&ChunkId(site_chunk)).unwrap();
            chunks.insert(ChunkId(site_chunk), failing(chunk, "site", placement));
            failure_witnesses(&solve_one(&chunks, &audited_host(), 0))
        };
        // A callable site runs whenever a published callback runs, except in a
        // chunk whose body starts only after the target finishes.
        assert_eq!(solve(1, SitePlacement::Callable), None);
        assert_eq!(solve(2, SitePlacement::Callable), Some(1));
        assert_eq!(solve(3, SitePlacement::Callable), Some(1));
        // A top-level load runs only while its module's body does.
        assert_eq!(solve(1, SitePlacement::ModuleBody), None);
        assert_eq!(solve(2, SitePlacement::ModuleBody), None);
        assert_eq!(solve(3, SitePlacement::ModuleBody), Some(1));
        assert_eq!(solve(0, SitePlacement::ModuleBody), Some(1));
    }

    #[test]
    fn an_importer_in_a_cycle_with_the_target_still_counts() {
        let chunks = BTreeMap::from([
            (ChunkId(0), surface(&[1], &[])),
            (
                ChunkId(1),
                failing(surface(&[0], &[]), "cycle", SitePlacement::Callable),
            ),
        ]);
        assert_eq!(
            failure_witnesses(&solve_one(&chunks, &audited_host(), 0)),
            Some(1)
        );
    }

    /// Chunk 2 runs before the target 0 and holds a failing site in its
    /// function node 1, exported as `f`.
    fn callable_program(
        pooled: bool,
        target_calls_f: bool,
        requested_by: Option<usize>,
    ) -> BTreeMap<ChunkId, ChunkLoaderSurface<&'static str>> {
        let mut target = surface(&[], &[]);
        target.callables.nodes = vec![CallableFacts {
            import_calls: if target_calls_f {
                vec![(ChunkId(2), "f".to_string())]
            } else {
                Vec::new()
            },
            ..CallableFacts::default()
        }];
        let mut owner = failing(surface(&[], &[]), "in f", SitePlacement::InFunction(1));
        owner.callables.nodes = vec![
            CallableFacts::default(),
            CallableFacts {
                pooled,
                ..CallableFacts::default()
            },
        ];
        owner.callables.exports = BTreeMap::from([("f".to_string(), 1)]);
        let mut chunks = BTreeMap::from([(ChunkId(0), target), (ChunkId(2), owner)]);
        if let Some(requester) = requested_by {
            chunks.insert(
                ChunkId(requester),
                surface(&[], &[(LoaderRequestTarget::Chunk(ChunkId(2)), "load 2")]),
            );
        }
        chunks
    }

    #[test]
    fn a_site_in_a_function_counts_only_when_the_window_can_enter_it() {
        let count = |chunks: &BTreeMap<ChunkId, ChunkLoaderSurface<&'static str>>| {
            failure_witnesses(&solve_one(chunks, &audited_host(), 0))
        };
        // Named, unexported to anything that runs, never called: excluded.
        assert_eq!(count(&callable_program(false, false, None)), None);
        // The target's top level calls it through its import.
        assert_eq!(count(&callable_program(false, true, None)), Some(1));
        // Its value escaped: any opaque call may invoke it.
        assert_eq!(count(&callable_program(true, false, None)), Some(1));
        // A loader request hands out chunk 2's namespace.
        assert_eq!(count(&callable_program(false, false, Some(3))), Some(1));
    }

    #[test]
    fn a_function_only_an_importer_of_the_target_calls_is_excluded() {
        let mut chunks = callable_program(false, false, None);
        let mut importer = surface(&[0], &[]);
        importer.callables.nodes = vec![CallableFacts {
            import_calls: vec![(ChunkId(2), "f".to_string())],
            ..CallableFacts::default()
        }];
        chunks.insert(ChunkId(4), importer);
        assert_eq!(
            failure_witnesses(&solve_one(&chunks, &audited_host(), 0)),
            None
        );
    }

    /// 2 loads 3 from a callback; 3's top-level site then runs in the window.
    #[test]
    fn a_counted_request_brings_the_loaded_chunk_into_the_window() {
        let chunks = BTreeMap::from([
            (ChunkId(0), surface(&[], &[])),
            (
                ChunkId(2),
                surface(&[], &[(LoaderRequestTarget::Chunk(ChunkId(3)), "2->3")]),
            ),
            (
                ChunkId(3),
                failing(surface(&[], &[]), "loaded", SitePlacement::ModuleBody),
            ),
        ]);
        let closure = solve_one(&chunks, &audited_host(), 0);
        assert_eq!(failure_witnesses(&closure), Some(1));
        assert_eq!(closure.loaded_chunks, BTreeSet::from([ChunkId(3)]));
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
