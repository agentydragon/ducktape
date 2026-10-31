# Claude CLI export-reentry blockers under the audited host

Question: with gaffer-private #667's audited `host_behavior`, which loader
blockers keep the canonical Claude CLI 2.1.292 graphs conservative, which can
the engine clear soundly, and what would clearing them buy? The decisions are
in <../plans/host_behavior.md> § Remaining loader blockers.

## Method

- **Engine run:** Ducktape `origin/devel` `5f30a13718`, built `-c opt`.
  - The spec is gaffer-private `origin/main` `288b0b718` (via `git archive`).
  - The input root and argv are the canonical `DebundlePipeline` replay used
    by the interpreter steps.
  - Each graph's `export_reentry.json` gives its policy, loader failures and
    static-closure blockers.
  - The run reproduces #667's figures: rejected, conflicts 3050, 1096, 800,
    586, 149, 117, 112, 88, 30, 18; 7,156,226 edges; 3,322
    `callback_pool_limit` losses; all 76 graphs conservative.
- **Site census:** an acorn parse of `$bunfs/root/*.js` gives each chunk's
  static imports and its loader calls (direct `import.meta.require` and the
  exported alias `He`), with literal specifiers and whether each call is in
  top-level code. Alias copies of chunks are excluded, so the counts are
  slightly below the engine's.

## Blockers per graph

Every graph has the program-wide failures `opaque_loader_site`,
`loader_factory_request`, `unresolvable_loader_request`,
`non_reentry_free_builtin_request` and `loaded_closure_request`.

| Graphs             | Count |     Edges | Pool losses |
| ------------------ | ----: | --------: | ----------: |
| With reach-back    |    44 | 6,484,441 |       2,630 |
| Without reach-back |    32 |   671,785 |         692 |

Static-closure blockers of the 32 graphs without reach-back:

| Blockers                          | Graphs |
| --------------------------------- | -----: |
| `process`, `ws`                   |     26 |
| `process`                         |      3 |
| none                              |      1 |
| `module`, `process`               |      1 |
| `process`, `worker_threads`, `ws` |      1 |

- **Conflicting graphs.** Nine of the 10 have reach-back: `cli`, `api`,
  `featureFlags`, `modelBetaDefs`, `taskCancel`, `remoteWorkerWire`,
  `toolCgroup`, `mcp` and `apiErrors`.
- **`sessionSDK`.** It is the only conflicting graph without reach-back. Its
  static closure needs `module` and `process`.
- **Largest graphs.** Among graphs without reach-back the largest is
  `agentForks` (613,422 edges). With reach-back, `cli` alone has 4,892,830.

Literal requests outside the closed list (engine total 119), from the census:

- assets: 71 `.md`, 25 `.txt`, 2 `.mjs`, 2 `.node`;
- Node builtins: `assert` 8, `querystring` 3, `constants` 1;
- Bun modules: `bun:ffi` 5, `undici` 3.

The 476 literal requests for chunks are Bun's lazy `require` of ESM chunks.
Of all 837 loader calls, 259 are in top-level code.

## Reach-back is real reentry

Public Bun 1.3.13 runs this repro. `main.mjs` dynamically imports `root.mjs`,
which statically imports `t.mjs`, so `t.mjs` evaluates through the static ESM
graph:

```javascript
// t.mjs
export function hoisted() {
  return "hoisted";
}
export const early = "early";
const loaded = import.meta.require(import.meta.dir + "/l.mjs");
export const late = "late";
// l.mjs (fresh when t.mjs requests it)
import * as t from "./t.mjs";
export const observed = {
  hoisted: t.hoisted(),
  early: t.early,
  late: (() => {
    try {
      return t.late;
    } catch (e) {
      return e.constructor.name;
    }
  })(),
};
```

`l.mjs`'s body runs inside `t.mjs`'s evaluation:
`{"hoisted":"hoisted","early":"early","late":"ReferenceError"}`.

A request for a fresh chunk that imports the target therefore hands the
target's partial namespace to code that runs on the target's stack. That is
exactly the reentry the conservative policy guards. This is indicative only:
the binding host is Bun 1.4.3 `eecfd55d`, and #667's probe covers only a
request for the evaluating module itself.

## Phase-bounded site rule (modelled)

The rule:

- A top-level site counts only when its chunk can evaluate inside the target's
  window: the target, its static closure, or a chunk loaded inside the window.
- A nested site counts only when its chunk could run before the target
  finishes, meaning it does not transitively import the target.
- Loads are iterated to a fixpoint.

Over the census this rule:

- cuts reach-back from 46 to 38 targets: `betaFlags`, `featureTrafficMode`,
  `mcpConnection`, `oauthEnv`, `sdkUrlOptions`, `taskCancel`,
  `voiceController` and `voiceStream` are freed;
- leaves the program-wide classes in 62 (`module`) to 75 targets.

## Premises the engine cannot derive

Exact wording for the author to accept or reject:

- **`process` (class 2):** "No code in the analyzed program defines or
  redefines an accessor property on `process` or on any object in its
  prototype chain (including `EventEmitter.prototype` and `Object.prototype`),
  or changes that prototype chain." With this premise, `process` and
  `worker_threads` can join `reentry_free_builtin_modules`.
- **Non-literal loader calls (class 3):** "Every non-literal loader call in
  the input either fails or loads a native addon, and no native addon's
  initializer calls application JavaScript or a module loader."

  The narrower alternative is a per-site assertion of each call's possible
  specifiers: `audio-capture` and `clipboard-napi` candidate paths, and
  protobufjs `inquire` names.
