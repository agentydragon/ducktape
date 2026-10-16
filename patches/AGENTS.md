# npm patches

Register npm patches in `package.json` under `pnpm.patchedDependencies`, add the
patch to `npm_translate_lock.data`, and commit the Bazel-generated `pnpm-lock.yaml`.
In rules_js 3.4.1, the custom `npm_translate_lock(patches=...)` path does not watch
patch contents; persistent Bazel servers can reuse older patched package sources.
