//! Rayon maps over work that may read SWC hygiene data.

use rayon::prelude::*;

/// Maps `jobs` in parallel, keeping their order. SWC's `GLOBALS` is a scoped
/// thread-local that rayon workers do not inherit, so each job runs inside
/// the caller's globals when the caller has set them.
pub(crate) fn map_with_swc_globals<Job: Send, Output: Send>(
    jobs: Vec<Job>,
    map: impl Fn(Job) -> Output + Sync,
) -> Vec<Output> {
    if swc_common::GLOBALS.is_set() {
        swc_common::GLOBALS.with(|globals| {
            jobs.into_par_iter()
                .map(|job| swc_common::GLOBALS.set(globals, || map(job)))
                .collect()
        })
    } else {
        jobs.into_par_iter().map(|job| map(job)).collect()
    }
}
