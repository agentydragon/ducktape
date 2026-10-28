# Target-local typed ESLint spike

The opt-in aspect uses each `ts_library`'s generated tsconfig and declared dependency types.
The shared ESLint configuration supplies the ordinary rules; the spike adds
`no-floating-promises`, `no-misused-promises` and `await-thenable` when the aspect supplies a
TypeScript project. The default aspect and existing whole-program lint tests remain enabled.

```sh
bbr build //util/frontend/x/typed_eslint:good //util/frontend/x/typed_eslint:worker \
  --aspects=//devinfra/lint/x/typed_eslint:aspect.bzl%eslint
```

The specimens below are valid TypeScript. The typed aspect is expected to reject each for its
named promise rule; `manual` tags keep these negative probes out of wildcard builds.

```sh
bbr build //util/frontend/x/typed_eslint:floating \
  //util/frontend/x/typed_eslint:misused //util/frontend/x/typed_eslint:non_thenable \
  --aspects=//devinfra/lint/x/typed_eslint:aspect.bzl%eslint --keep_going
```

`good` awaits a Promise imported from another library and one imported from a generated
declaration. `worker` extends a separate config with WebWorker types. Neither package declares
a lint source filegroup. The rules_ts patch wraps the generated config in `ts_config`, with its
extended config in `deps`. The aspect gathers these through its existing `JsInfo` inputs.
The remaining rules_lint patch exposes the config path through `BAZEL_ESLINT_TSCONFIG` and gives
typed reports distinct names so the normal and experimental aspects can run together.

See [the investigation](../../../debug/typed_eslint_aspect.md) for proof, real-target coverage,
observed cost, and the remaining adoption gates.
