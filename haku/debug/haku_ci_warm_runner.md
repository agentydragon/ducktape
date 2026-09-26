# Haku CI Warm Bazel Runner Option

## Short answer

Yes, Forgejo can support a warm-runner shape, but not with the current
`docker://catthehacker/ubuntu:act-latest` job model if the goal is a long-lived
Bazel server.

The current haku-ci runner creates a fresh job container for each workflow job.
That job container gets a clean process tree, so the Bazel server dies at the end
of each run even if the runner pod and rootless dind sidecar stay warm. The
existing disk cache helps downloaded/action outputs, but it does not keep Bazel
analysis state or a live server process warm.

## What would work

### Option A: keep current model, improve persistent caches

This is the lowest-risk option.

- Keep Forgejo Actions jobs as `docker://...` containers.
- Mount a persistent volume for the runner/dind cache directories.
- Keep Bazel disk cache, repository cache, pnpm/npm cache, and maybe
  `~/.cache/bazel` warm across runs.
- Still no persistent Bazel server, because each job container exits.

This improves cold fetch/build time, but it will not eliminate Bazel startup or
analysis.

### Option B: custom job image plus persistent caches

This is likely the practical next step.

- Build a custom haku-ci job image with:
  - Bazelisk already installed.
  - Java truststore already containing the haku egress CA.
  - proxy environment and CA bundle baked in.
  - common tools already present.
- Keep the `docker://custom-image` Forgejo runner model.
- Add persistent cache volumes where Forgejo runner allows them.

This removes repeated setup work and reduces external fetches, but still does
not keep a live Bazel server across workflow jobs.

### Option C: shell/host-style runner for true warm Bazel server

This is the only option that plausibly gives a live Bazel daemon across runs.

- Run jobs directly on a long-lived runner filesystem instead of inside
  ephemeral Docker job containers.
- Give the runner a persistent workspace/home.
- Let `bazel` reuse the same output base and server between jobs.

Tradeoff: this is a much larger trust-boundary change. Haku-authored workflow
code would execute directly in the long-lived runner environment. That means one
run can poison state for later runs unless we add our own cleanup and isolation.
For haku-state, where agent-authored code is treated as adversarial, this is
materially riskier than the current per-job container model.

## Forgejo support question

Forgejo Actions/act_runner supports long-lived runners and persistent runner
pods, and the runner config already supports Docker-backed job execution with a
shared dind daemon. The part that is not provided by the current Docker-backed
job mode is a persistent in-job process namespace.

So:

- **Warm runner pod:** yes, already true.
- **Warm rootless Docker daemon:** yes, already true.
- **Warm Bazel disk/repository cache:** yes, feasible.
- **Warm live Bazel server inside the job:** not with fresh `docker://...` job
  containers.
- **Warm live Bazel server via host/shell execution:** likely feasible, but it
  weakens isolation and needs deliberate cleanup/hardening.

## Recommendation

Do not jump straight to shell/host execution for haku-ci.

First, use Option B:

1. Bake a custom haku-ci job image with Bazelisk, CA trust, and common tools.
2. Keep the Docker job-container isolation.
3. Add persistent caches for Bazel disk/repository cache and
   frontend/package-manager caches.
4. Measure whether the remaining time is still dominated by Bazel
   analysis/server startup.

Only consider shell/host execution if the custom image plus persistent caches is
still too slow and we are comfortable treating the runner workspace as
contaminated between jobs, with explicit cleanup and a narrower workload surface.

## Current incident note

The current image-push fix exposed two separate costs:

- cold Bazel fetch/analysis needs egress for its full source/toolchain closure;
- the push step still needs correct Forgejo registry auth.

A warm runner would reduce repeated cold fetch pain, but it would not fix
allowlist gaps or registry credential propagation by itself.
