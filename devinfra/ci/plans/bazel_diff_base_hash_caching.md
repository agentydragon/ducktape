# Cross-run bazel-diff base-hash caching: deferred

The July proposal is superseded by the
[measurement and revisit criteria](../debug/bazel_diff_base_hash_cache.md).
Do not revive it from the duration of the base hash command alone: the base and
candidate queries share warm Bazel state, and skipping the base can move work
to the candidate or subsequent analysis.

The [original transport sketch](https://github.com/agentydragon/ducktape/blob/1cebdb0f1f/devinfra/ci/plans/bazel_diff_base_hash_caching.md)
remains in Git history. Its 25–35 s per-call estimate and claimed 30–60 s saving
were not a matched cache-hit experiment. A SHA alone is also not proof that two
producers used equivalent Bazel, bazel-diff, repository and configuration inputs.
