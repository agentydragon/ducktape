# Tests that read Flux manifest trees

Snapshot of live `devel` at `dde6abdbaa5d51a9688a378d6c7827193287b69f`, confirmed through Agentplane's GitHub Actions. Rows are Bazel test targets; a target may contain several test cases. This report changes documentation only.

Tests should read the source they are meant to validate. Most cdk8s behavior can be tested from focused constructs or in-memory synth output. Keep the cdk8s parity check against the files Flux consumes until cutover; that one relation does not make a shared rendered input necessary for other tests. Whole-graph checks belong with the owner of the Flux source graph.

| Class | Meaning |
| --- | --- |
| a | Change detector or restatement of declarations; delete candidate. |
| b | Durable relation between source or rendered artifacts. |
| c | Rendered schema/policy validation or full Flux graph validation. |
| d | Generator/model behavior that can run on cdk8s output in memory. |
| e | Other; the row explains the consumer-specific reason. |

## Inventory

<!-- prettier-ignore -->
| Test path and Bazel target | Files read and assertion | Class | Recommended home | Real regression evidence |
| --- | --- | --- | --- | --- |
| `cluster/cdk8s/test_generate_manifests.py`; `//cluster/cdk8s:test_generate_manifests` | Synthesizes cdk8s output, reads committed twins under `cluster/generated/` and generated `*.k8s.yaml` under `cluster/k8s/` and `cluster/parked/`, and loads the source inputs needed by the generator. The consolidated tree comparison reports generated files missing from the committed output, content differences, and committed extras under the closed generated root; other cases assert generator invariants on the temporary synth. | b (also d) | Keep the single cdk8s-output-to-Flux-files comparison through cutover. At cutover, remove that comparison only; keep generator behavior assertions on in-memory output. | No specific regression is cited by the test. |
| `cluster/cdk8s/authentik/test_blueprint_contracts.py`; `//cluster/cdk8s/authentik:test_blueprint_contracts` | Reads authored `cluster/k8s/authentik/app/blueprints/*.yaml` through the blueprint package's filegroup. Asserts every present proxy provider is assigned to an outpost and that present outpost provider references resolve without targeting retired entries. | b | Keep beside the Authentik source/model package and pass only its blueprint inputs. It does not need the root Flux Kustomization or a cluster render. | [#2390](https://github.com/agentydragon/ducktape/pull/2390) documents provider wiring that had caused redirect-URI failures for two hosts. |
| `cluster/cdk8s/haku/test_mailbox.py`; `//cluster/cdk8s/haku:test_mailbox` | Reads `cluster/k8s/haku/mailbox/nginx.conf` (a source config file, not YAML), synthesizes the mailbox Service, and asserts the SMTP proxy protocol setting and destination service name, namespace, and port. Its other case tests mailbox initialization from the model alone. | b | Keep with the Haku mailbox model and its specific ingress config input. If the config's source location changes, move that input with its owner. | No specific regression or issue is cited. |
| `cluster/cdk8s/haku_ci/test_secret_contract.py`; `//cluster/cdk8s/haku_ci:test_secret_contract` | Reads the authored `cluster/k8s/haku/forgejo-tea/haku-forgejo-tea.sops.yaml`. Checks Secret name/key against the runner model and verifies the Reflector annotations target the KEDA namespace. | b | Keep as a narrow source-Secret contract beside the Haku CI runner model; do not load a rendered copy or the whole graph. | No specific regression or issue is cited. |
| `cluster/validation/test_cluster_integration.py`; `//cluster/validation:test_cluster_integration` | Through `conftest.py`, `parse_cluster`, and its Bazel data closure, reads the complete `cluster/k8s/` and `cluster/generated/` trees plus package-local manifest groups. Checks full-graph build coverage, dependencies, health checks, credential ownership, image/namespace and ImageRepository/Receiver relations, image-policy markers, Flux rewrite and bootstrap constraints, SOPS decryption, orphan files, Agentplane namespace-owner/delegation dependencies, and absence of retired readers. | b (mixed with c) | Keep complete-graph assertions with the Flux graph owner. Extract an assertion only when a focused source-model test proves the same contract; the Agentplane grant/model checks already live at the cdk8s layer. | The image-policy marker check cites [#3882](https://github.com/agentydragon/ducktape/pull/3882), where a stale marker stopped an image from updating. The Flux rewrite check records three formatting regressions in its source comment. |
| `cluster/validation/test_flux_build.py`; `//cluster/validation:test_flux_build` | Uses the root `cluster/k8s/kustomization.yaml` and Bazel data for the referenced hand-written and generated trees, including package-local groups. Runs `flux build` and asserts the complete source graph builds and contains Kustomization and GitRepository resources. | c | Keep with the source graph owner and run against that graph's source tree. This is a graph build check, not a consumer of a shared CI render. | No specific regression or issue is cited. |
| `cluster/validation/test_generator_namespace.py`; `//cluster/validation:test_generator_namespace` | Uses the root Kustomization as a runfiles anchor and reads hand-written `kustomization.yaml` files under `cluster/k8s/` plus central Flux Kustomizations. Checks active Flux targets using ConfigMap/Secret generators declare a namespace locally or through `targetNamespace`; it skips generated-root files before reading their contents. The target still has an unnecessary `//cluster:generated_manifests` data dependency. | b | Keep the source-declaration check with the hand-written Kustomizations. Remove the unused generated-manifests data dependency in a small cleanup; test cdk8s-generated declarations at the model layer. | The test comment records a three-hour public-coder 502 outage on 2026-07-29 after a generator was applied without a namespace. |
| `cluster/validation/test_public_coder_clickhouse_reader_contract.py`; `//cluster/validation:test_public_coder_clickhouse_reader_contract` | Reads the authored `cluster/k8s/clickhouse/cluster/public-coder-credentials.sops.yaml`. Checks Secret name, namespace, password key, and absence of Reflector annotations against `cluster.cdk8s.clickhouse.client` constants. | b | Keep the exact Secret source contract with the ClickHouse client model; the target is a candidate to move from `cluster/validation` into `cluster/cdk8s/clickhouse`. Do not load a rendered Secret or whole cluster. | No specific regression or issue is cited. |

Counts assign one primary class to each Bazel target. The integration target is mixed because it checks artifact relations and rendered policy/graph properties; the generator target is also d.

| Primary class | Test targets |
| --- | ---: |
| a — change detector / restatement | 0 |
| b — artifact relations | 7 |
| c — rendered schema / policy validation | 1 |
| d — generator/model behavior | 0 |
| e — other | 0 |
| **Total** | **8** |

Three targets read generated Flux output as part of their actual work: `test_generate_manifests`, `test_cluster_integration`, and `test_flux_build`. `test_generator_namespace` carries the generated tree in runfiles but skips the generated root before opening YAML; its data edge can be dropped. The four focused source contracts read only Authentik blueprints, Haku's SMTP config, the Haku CI Secret, or the ClickHouse Secret.

## Searches and completeness

I used Agentplane GitHub code search scoped to `agentydragon/ducktape` on live `devel`. These were the queries:

```text
repo:agentydragon/ducktape cluster/generated
repo:agentydragon/ducktape cluster/k8s
repo:agentydragon/ducktape cluster/k8s language:Python
repo:agentydragon/ducktape cluster/generated path:cluster/validation
repo:agentydragon/ducktape cluster/k8s path:cluster/validation
repo:agentydragon/ducktape generated_manifests path:cluster
repo:agentydragon/ducktape cluster_all_files path:cluster
repo:agentydragon/ducktape manifest_files path:cluster
repo:agentydragon/ducktape k8s_dir path:cluster
repo:agentydragon/ducktape generated_dir path:cluster
repo:agentydragon/ducktape cluster/k8s OR cluster/generated language:Go
repo:agentydragon/ducktape cluster/k8s OR cluster/generated language:TypeScript
repo:agentydragon/ducktape cluster/k8s OR cluster/generated language:Shell
```

The `cluster/generated` query returned 59 results; the matching test sources were `test_cluster`, `test_generate_manifests`, and `test_cluster_integration`. The Python `cluster/k8s` query returned 93 results. I followed candidates to their opens and Bazel targets, including the `generated_manifests` and `cluster_all_files` data edges, package-boundary filegroups, and shared loaders. No Go, TypeScript, or shell source matched either tree.

For reproduction from a local checkout containing the snapshot commit, these broad `git grep` scans include the test suffixes and runfiles/helper names:

```sh
git grep -n -E 'cluster/(k8s|generated)|manifest_files\(|k8s_dir|generated_dir|GENERATED_ROOT|repo_root|_main/cluster/(k8s|generated)/' dde6abdbaa5d51a9688a378d6c7827193287b69f -- \
  ':(glob)**/test*.py' ':(glob)**/*_test.py' \
  ':(glob)**/*_test.go' ':(glob)**/*.test.ts' ':(glob)**/*.spec.ts' \
  ':(glob)**/*_test.ts' ':(glob)**/*_test.tsx' \
  ':(glob)**/*test*.sh' ':(glob)**/*.bats' ':(exclude,glob)props/**'
```

The Bazel data-edge search includes package-boundary globs and named filegroups:

```sh
git grep -n -E 'cluster/(k8s|generated)|generated_manifests|parked_generated_manifests|cluster_all_files|flux_bootstrap_files|flux_system_files|authentik_blueprints|agentplane-staging:manifests|proxy_manifest' dde6abdbaa5d51a9688a378d6c7827193287b69f -- \
  ':(glob)**/BUILD.bazel' ':(glob)**/BUILD' ':(glob)**/*.bzl' ':(exclude,glob)props/**'
```

I traced shared loaders and anchors through `cluster/validation/conftest.py`, `cluster/validation/cluster.py`, `cluster/cdk8s/manifest_roots.py`, and `util.bazel.runfiles.get_required_path`:

```sh
git grep -n -E '(_main/)?cluster/(k8s|generated)/|manifest_files\(|GENERATED_ROOT|generated_dir|k8s_dir|BLUEPRINTS_DIR_ANCHOR|proxy_manifest' dde6abdbaa5d51a9688a378d6c7827193287b69f -- \
  cluster/validation cluster/cdk8s cluster/rotators agentplane/system_tests
```

The equivalent local scan for non-Python test files is:

```sh
git grep -n -E 'cluster/(k8s|generated)|manifest_files\(|k8s_dir|generated_dir|GENERATED_ROOT' dde6abdbaa5d51a9688a378d6c7827193287b69f -- \
  ':(glob)**/*_test.go' ':(glob)**/*.test.ts' ':(glob)**/*.spec.ts' \
  ':(glob)**/*_test.ts' ':(glob)**/*_test.tsx' ':(glob)**/*test*.sh' ':(glob)**/*.bats' \
  ':(exclude,glob)props/**'
```

False positives were checked and excluded: `test_render_diff` creates temporary Git trees; `test_cluster`, `test_flux`, `test_checks`, `test_crd_layering`, `test_dependencies`, `test_health_checks`, and `test_sops_decryption` use temporary paths or synthetic resources. The Authentik and Forgejo rotator tests write to `tmp_path`. `test_image_registry` mentions `cluster/k8s/**` in a docstring while asserting literal URLs; `test_publisher` uses a Bazel target-name string; container E2E stages a fixture from `testdata/e2e_secrets`. `test_egress_fences` and Agentplane egress tests mention tree paths in comments or model metadata but synthesize in memory. The LiteLLM parser test uses explicit config fixtures; its production binary's manifest data is not a test data dependency.

## Recommended next directions

The first two items are small candidate PRs. Keep each independent so it can be reviewed on its own. The rest are focused coverage reviews or cutover work.

1. **Drop the unused generated-tree data edge.** Remove `//cluster:generated_manifests` from `//cluster/validation:test_generator_namespace`; preserve its hand-written Kustomization check.
2. **Place the ClickHouse source contract with its model.** Move `test_public_coder_clickhouse_reader_contract` into `cluster/cdk8s/clickhouse`, retaining only its authored Secret input from the source package.
3. **Test Forgejo ImageRepository/Receiver wiring in the source model.** Add focused cdk8s coverage for the shared CI image roster and Receiver references. Remove the duplicated whole-graph assertion only if the focused tests cover both directions.
4. **Assess Forgejo image credential coverage.** Check whether focused cdk8s synthesis covers every workload and reflected pull Secret in `test_forgejo_image_namespaces_are_reflected`. Keep residual graph coverage for Helm or non-cdk8s workloads.
5. **Compare Goldilocks checks for overlap.** `test_goldilocks_namespace_labels` validates the rendered namespace set; compare it with namespace-constructor coverage and remove only proven duplication.
6. **Compare Cilium policy checks with synth-time validation.** `test_cilium_policy_rules_nonempty` also sees hand-written or post-Kustomize resources; retain that coverage unless the source-model tests cover those inputs.
7. **Review controller health and retry checks individually.** Their predicates concern Flux Kustomization specs and applied resources. Move an assertion only when source-model tests cover every relevant Kustomization.
8. **Keep image-marker and rewrite-format checks with Flux source.** They inspect YAML comments and exact source text that Flux rewrites. Move them with the authored Flux tree when its owner changes.
9. **Keep graph-structure checks with the graph owner.** `test_flux_build`, dependency resolution, orphan detection, SOPS decryption, and namespace-owner/delegation dependencies require the actual Flux graph.
10. **At cutover, remove only the parity comparison.** Keep generator invariants on in-memory output and move graph checks to the Flux source owner's pipeline.

The remaining ownership question is where authored inputs under `cluster/k8s/`—blueprints, SOPS Secrets, SMTP config, and hand-written Kustomizations—will live after Flux delivery leaves `devel`. Their contract tests should follow those source inputs. The answer affects package ownership, not the two immediately actionable trim candidates above.
