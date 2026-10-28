# Typed ESLint with the stock aspect

The experiment selects a custom ESLint config through the existing aspect's binary.
It uses `parserOptions.project: ["**/tsconfig_*.json"]` to discover generated projects in
Bazel's declared inputs. The config retains the ordinary rules and adds
`no-floating-promises`, `no-misused-promises`, and `await-thenable`.

```sh
bbr build //util/frontend/x/typed_eslint:good //util/frontend/x/typed_eslint:worker \
  --//devinfra/lint/x/typed_eslint:enabled
```

These valid TypeScript specimens should fail for their named promise rules;
`manual` tags keep them out of wildcard builds.

```sh
bbr build //util/frontend/x/typed_eslint:floating \
  //util/frontend/x/typed_eslint:misused //util/frontend/x/typed_eslint:non_thenable \
  --//devinfra/lint/x/typed_eslint:enabled --keep_going
```

`ts_library` wraps each generated config in `ts_config`, with its base config in `deps`,
and supplies that wrapper as a library dependency. The aspect's existing `JsInfo` gathering
therefore supplies the generated config and its extended configs. Neither the aspect nor
rules_ts needs a new upstream patch, and callers need no additional lint source filegroups.

The flag defaults to false. Existing whole-program typed lint tests remain enabled.
There is one ESLint aspect and one set of report names in either mode.

See [the investigation](../../../debug/typed_eslint_aspect.md) for commands, evidence, cost,
and the remaining adoption gates.
