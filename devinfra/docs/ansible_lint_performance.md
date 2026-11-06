# Ansible-Lint Performance (Historical)

## Current checks and setup

Pre-commit's root-level playbook filter is in
[`.pre-commit-config.yaml`](../../.pre-commit-config.yaml); it runs
`ansible-playbook --syntax-check` through
[`run-syntax-check.sh`](../../ansible/scripts/run-syntax-check.sh). CI runs full
`ansible-lint` (style, best-practice, deprecation and security rules) when `ansible/`
changes; the
[`ansible-lint` workflow](../../.github/workflows/ansible-lint.yml) installs Python,
Ansible and `ansible-lint`, then the project's Galaxy roles and collections, before
calling [`run-ansible-lint.sh`](../../.github/scripts/run-ansible-lint.sh). The
[linting guide](linting.md#ci-boundaries) describes this split and its owners.

For a focused local run, use `cd ansible` followed by:

```bash
ansible-lint --project-dir . --config-file ../.ansible-lint.yaml [playbook.yaml]
```

## Earlier profiling and unsubmitted proposals

The figures below come from earlier notes, not current CI timings. The
optimization proposals were recorded as unsubmitted. Source locations are those
recorded by the profile.

| Area and profile evidence                                                                                                                                                                 | Proposed optimization                                                                                       | Estimated saving | Recorded complexity / risk |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------------- | -------------------------- |
| `get_deps_versions()` at `src/ansiblelint/config.py:282`: 3,652 calls per run.                                                                                                            | Add `@functools.cache`.                                                                                     | 5–6s             | Trivial / none             |
| `kind_from_path()` at `src/ansiblelint/file_utils.py:139`: 39,334 `posix.stat()` calls, up to 7 per file.                                                                                 | Cache file-type detection with `@functools.lru_cache(maxsize=1024)`.                                        | 2–3s             | Trivial / low              |
| `_sanitize_task()` at `src/ansiblelint/utils.py:712`: 1.3 million `copy.deepcopy()` calls.                                                                                                | Copy the task's top level, then copy only mutable nested values that need it.                               | 1.5–2s           | Medium / medium            |
| Subprocess work: `ansible-config dump`, `ansible --version`, and `ansible-galaxy` collection install/list calls took about 0.75s each; syntax checks were measured at 1.15s per playbook. | Cache collection metadata, skip version checks in offline mode, and cache `ansible-doc` module information. | 2–4s             | Medium / low               |

The profile summary estimated 11–15s total savings, moving a roughly 42s run to
27–31s; it said about 20s of per-playbook subprocess overhead would remain. Earlier
notes recorded syntax checks at 1–3s per playbook, and the profile measured them at
0.6–2s (using 1.15s in its subprocess estimate). The same note recorded 191 files at
0.22s per file and compared that run with zuul-roles (330+ files in 80s). These are
historical estimates and comparisons, not current benchmarks or upstream guarantees.

The earlier notes attributed repeated startup to the per-playbook syntax check
([upstream discussion](https://github.com/ansible/ansible-lint/discussions/1256));
recheck upstream before treating that batching limitation as current.

One environment caveat from that profile: when
`python3 -c "import yaml; print(yaml.__with_libyaml__)"` returned `False`, using a
PyYAML build with libyaml was reported to save a few seconds.
