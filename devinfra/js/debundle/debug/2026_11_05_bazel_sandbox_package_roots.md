# Bazel sandbox package-root investigation (2026-11-05)

`pipeline.bzl` sets `no-sandbox` on all four Debundle actions. The stated reason is a real path mismatch in Bazel's symlink-based local sandbox: `vendor/packages.rs::resolve_package_subpath` checks that the canonical vendor subpath stays under the canonical package root, while the sandbox may make the root a directory and each input leaf a symlink into the host execroot. Removing the execution requirement alone would reject valid package inputs. This investigation did not establish a sound, validated replacement; keep the requirement until the checks below pass.

## Observed mechanism

A minimal Bazel 9.2 workspace used one Starlark action to write a declared directory artifact containing `package.json`, `index.js`, and an authored `escape.js -> /etc/passwd` symlink. A second `run_shell` action consumed that directory under `processwrapper-sandbox` and recorded `ls -ld`, `readlink`, and `realpath` for the root and leaves. The invocation was:

```sh
bazelisk --output_base=/tmp/agents/2026_11_05/bazel-review/sandbox/probe-out build //:package_probe \
  --strategy=PackageGenerate=processwrapper-sandbox \
  --strategy=PackageProbe=processwrapper-sandbox --sandbox_debug
```

The consuming action saw a real directory at `bazel-out/k8-fastbuild/bin/package`. Its three leaves were symlinks whose first hop pointed to the corresponding path under the host `output_base/execroot/_main/bazel-out/k8-fastbuild/bin/package`. `realpath(package)` stayed under the action sandbox; `realpath(index.js)` reached the host execroot; and `realpath(escape.js)` reached `/etc/passwd`. Thus the existing canonical check rejects both the legitimate leaf and the authored escape in that sandbox. The probe preserved the authored link as a distinct second hop, which matters for any repair. Bazel's [sandboxing documentation](https://bazel.build/docs/sandboxing) describes the symlink forest used by `processwrapper-sandbox`; `linux-sandbox` builds on it.

The local Bazel client could not run a `linux-sandbox` action: Bazel reported that this strategy was not registered on this host. The `processwrapper-sandbox` result establishes the path mechanism, not a pass under `linux-sandbox`. Remote BuildBuddy actions also do not establish local sandbox behavior.

## Constraints on a repair

The canonical containment check prevents a vendor subpath from following an authored symlink outside its package. Copying sandbox leaves with `cp -L` would turn such an escape into a regular file inside the copied tree and silently weaken the check. A copy of the host package tree that preserves symlinks could retain the check, but would copy every mapped package for projection, analysis, and emission; a private downstream pipeline maps roughly fifty package roots. It also needs to handle absolute package-internal links. The cost and compatibility of that approach remain unmeasured.

An exploratory Rust change recognized the host execroot from the action cwd and each leaf's first symlink hop, then compared physical host paths. It was discarded: generic vendor resolution should not infer Bazel execution policy from cwd spelling. A deliberate Bazel adapter would need an explicit trust anchor from the action, exact mapping checks for each declared leaf, and rejection when a package supplies a lookalike or mismatched link. The normal CLI's canonical containment rule should remain unchanged.

A private companion repo has its own cached-pipeline action with the same `no-sandbox` requirement. A current-main BuildBuddy run reached that action but failed earlier on an unrelated stale synthetic ownership-subset fixture (`missing field modules`); it provides no package-path acceptance evidence. A Ducktape-only edit would therefore leave one consumer workaround in place.

## Next experiment

Add a small public `debundle_pipeline` fixture with a generated package-root tree and a vendor swap. On a host or runner where `linux-sandbox` is registered, temporarily remove the execution requirement and force `DebundleProjectAnalysis`, `DebundleAnalyze`, and `DebundlePipeline` to use that strategy. Demonstrate the failure before changing path handling. Then require a passing valid package, a passing package-internal symlink, and rejection of an authored escape, a lookalike host tree, and a mismatched package root. Run the same candidate against the private package-root pipeline and its separately defined cached action, without publishing its source or outputs. Only then remove both requirements.
