# Tests that read Flux manifest trees

Snapshot of live `devel` at `51339f2e89e9c0693ccfdf10297afbd49a4e7333`, confirmed through Agentplane's GitHub Actions. Rows are Bazel test targets; a target may contain several test cases. The preceding image-pin sync changes one source Kustomization but adds no test target or data edge. This report changes documentation only.

Tests should read the source they are meant to validate. Most cdk8s behavior can be tested from focused constructs or in-memory synth output. Keep the cdk8s parity check against the files Flux consumes until cutover; that one relation does not make a shared rendered input necessary for other tests. Whole-graph checks belong with the owner of the Flux source graph.

| Class | Meaning                                                           |
| ----- | ----------------------------------------------------------------- |
| a     | Change detector or restatement of declarations; delete candidate. |
| b     | Durable relation between source or rendered artifacts.            |
| c     | Rendered schema/policy validation or full Flux graph validation.  |
| d     | Generator/model behavior that can run on cdk8s output in memory.  |
| e     | Other; the row explains the consumer-specific reason.             |

## Inventory

<!-- prettier-ignore -->
| Test path and Bazel target | Files read and assertion | Class | Recommended home | Real regression evidence |
| --- | --- | --- | --- | --- |
| `cluster/cdk8s/test_generate_manifests.py`; `//cluster/cdk8s:test_generate_manifests` | Synthesizes cdk8s output, reads committed twins under `cluster/generated/` and generated `*.k8s.yaml` under `cluster/k8s/` and `cluster/parked/`, and loads the source inputs needed by the generator. The consolidated tree comparison reports generated files missing from the committed output, content differences, and committed extras under the closed generated root; other cases assert generator invariants on the temporary synth. | b (also d) | Keep the single cdk8s-output-to-Flux-files comparison through cutover. At cutover, remove that comparison only; keep generator behavior assertions on in-memory output. | No specific regression is cited by the test. |
| `cluster/cdk8s/authentik/test_blueprint_contracts.py`; `//cluster/cdk8s/authentik:test_blueprint_contracts` | Reads authored `cluster/k8s/authentik/app/blueprints/*.yaml` through the blueprint package's filegroup. Asserts every present proxy provider is assigned to an outpost and that present outpost provider references resolve without targeting retired entries. | b | Keep beside the Authentik source/model package and pass only its blueprint inputs. It does not need the root Flux Kustomization or a cluster render. | [#2390](https://github.com/agentydragon/ducktape/pull/2390) documents provider wiring that had caused redirect-URI failures for two hosts. |
| `cluster/cdk8s/haku/test_mailbox.py`; `//cluster/cdk8s/haku:test_mailbox` | Reads `cluster/k8s/haku/mailbox/nginx.conf` (a source config file, not YAML), synthesizes the mailbox Service, and asserts the SMTP proxy protocol setting and destination service name, namespace, and port. Its other case tests mailbox initialization from the model alone. | b | Keep with the Haku mailbox model and its specific ingress config input. If the config's source location changes, move that input with its owner. | No specific regression or issue is cited. |
| `cluster/cdk8s/haku_ci/test_secret_contract.py`; `//cluster/cdk8s/haku_ci:test_secret_contract` | Reads the authored `cluster/k8s/haku/forgejo-tea/haku-forgejo-tea.sops.yaml`. Checks Secret name/key against the runner model and verifies the Reflector annotations target the KEDA namespace. | b | Keep as a narrow source-Secret contract beside the Haku CI runner model; do not load a rendered copy or the whole graph. | No specific regression or issue is cited. |
| `cluster/validation/test_cluster_integration.py`; `//cluster/validation:test_cluster_integration` | Through `conftest.py`, `parse_cluster`, and its Bazel data closure, reads the complete `cluster/k8s/` and `cluster/generated/` trees plus package-local manifest groups. Checks full-graph build coverage, dependencies, health checks, credential ownership, image/namespace and ImageRepository/Receiver relations, image-policy markers, Flux rewrite and bootstrap constraints, SOPS decryption, orphan files, Agentplane namespace-owner/delegation dependencies, and absence of retired readers. `test_files_flux_rewrites_use_block_style` and `test_flux_bootstrap_sources_need_no_decrypted_auth` inspect authored source files only but share this target's generated-tree data deps. | b (mixed with c) | Keep complete-graph assertions with the Flux graph owner. Extract the two source-only checks into a focused target with only their authored inputs; extract any other assertion only when a focused source-model test proves the same contract. Agentplane grant/model checks already live at the cdk8s layer. | The image-policy marker check cites [#3882](https://github.com/agentydragon/ducktape/pull/3882), where a stale marker stopped an image from updating. The Flux rewrite check records three formatting regressions in its source comment. |
| `cluster/validation/test_flux_build.py`; `//cluster/validation:test_flux_build` | Uses the root `cluster/k8s/kustomization.yaml` and Bazel data for the referenced hand-written and generated trees, including package-local groups. Runs `flux build` and asserts the complete source graph builds and contains Kustomization and GitRepository resources. | c | Keep with the source graph owner and run against that graph's source tree. This is a graph build check, not a consumer of a shared CI render. | No specific regression or issue is cited. |
| `cluster/validation/test_generator_namespace.py`; `//cluster/validation:test_generator_namespace` | Uses the root Kustomization as a runfiles anchor and scans hand-written `kustomization.yaml` files under `cluster/k8s/` plus central Flux Kustomizations. Checks active Flux targets using ConfigMap/Secret generators declare a namespace locally or through `targetNamespace`. It enumerates manifest roots but skips generated-root entries before opening them; its data deps include only the hand-written roots it reads. | b | Keep the source-declaration check with the hand-written Kustomizations. Test cdk8s-generated declarations at the model layer. | The test comment records a three-hour public-coder 502 outage on 2026-07-29 after a generator was applied without a namespace. |
| `cluster/cdk8s/clickhouse/test_public_coder_clickhouse_reader_contract.py`; `//cluster/cdk8s/clickhouse:test_public_coder_clickhouse_reader_contract` | Reads the authored `cluster/k8s/clickhouse/cluster/public-coder-credentials.sops.yaml`. Checks Secret name, namespace, password key, and absence of Reflector annotations against `cluster.cdk8s.clickhouse.client` constants. Moved beside the model in [#11995](https://github.com/agentydragon/ducktape/pull/11995). | b | Keep the exact Secret source contract with the ClickHouse client model. It reads the authored Secret only, not a rendered copy or whole cluster. | No specific regression or issue is cited. |

Counts assign one primary class to each Bazel target. The integration target is mixed because it checks artifact relations and rendered policy/graph properties; the generator target is also d.

| Primary class                           | Test targets |
| --------------------------------------- | -----------: |
| a — change detector / restatement       |            0 |
| b — artifact relations                  |            7 |
| c — rendered schema / policy validation |            1 |
| d — generator/model behavior            |            0 |
| e — other                               |            0 |
| **Total**                               |        **8** |

Three targets read generated Flux output as part of their actual work: `test_generate_manifests`, `test_cluster_integration`, and `test_flux_build`. `test_generator_namespace` enumerates manifest roots but skips generated-root entries before opening YAML; its data deps contain only the hand-written inputs it reads. The four focused source contracts read only Authentik blueprints, Haku's SMTP config, the Haku CI Secret, or the ClickHouse Secret.

## Searches and completeness

I used Agentplane GitHub code search scoped to `agentydragon/ducktape` on live `devel`. These were the queries:

```text
repo:agentydragon/ducktape cluster/generated
repo:agentydragon/ducktape cluster/k8s
repo:agentydragon/ducktape cluster/k8s language:Python
repo:agentydragon/ducktape cluster/generated path:cluster/validation
repo:agentydragon/ducktape cluster/k8s path:cluster/validation
repo:agentydragon/ducktape cluster/k8s path:cluster/cdk8s
repo:agentydragon/ducktape generated_manifests path:cluster
repo:agentydragon/ducktape cluster_all_files path:cluster
repo:agentydragon/ducktape manifest_files path:cluster
repo:agentydragon/ducktape k8s_dir path:cluster
repo:agentydragon/ducktape generated_dir path:cluster
repo:agentydragon/ducktape cluster/k8s OR cluster/generated language:Go
repo:agentydragon/ducktape cluster/k8s OR cluster/generated language:TypeScript
repo:agentydragon/ducktape cluster/k8s OR cluster/generated language:Shell
```

The broad searches include documentation, comments, generated manifests, and source references, so I used them to find candidates rather than count readers. I followed relevant hits to their file opens and Bazel targets, including transitive data edges, package-boundary filegroups, and shared loaders. The live validation BUILD has generated-tree data edges only for `test_cluster_integration` and `test_flux_build`; `test_generator_namespace` has no generated-tree data dependency. Code searches for Go, TypeScript, and shell test references returned no matches.

For reproduction from a local checkout containing the snapshot commit, these broad `git grep` scans include the test suffixes and runfiles/helper names:

```sh
git grep -n -E 'cluster/(k8s|generated)|manifest_files\(|k8s_dir|generated_dir|GENERATED_ROOT|repo_root|_main/cluster/(k8s|generated)/' 51339f2e89e9c0693ccfdf10297afbd49a4e7333 -- \
  ':(glob)**/test*.py' ':(glob)**/*_test.py' \
  ':(glob)**/*_test.go' ':(glob)**/*.test.ts' ':(glob)**/*.spec.ts' \
  ':(glob)**/*_test.ts' ':(glob)**/*_test.tsx' \
  ':(glob)**/*test*.sh' ':(glob)**/*.bats' ':(exclude,glob)props/**'
```

The Bazel data-edge search includes package-boundary globs and named filegroups:

```sh
git grep -n -E 'cluster/(k8s|generated)|generated_manifests|parked_generated_manifests|cluster_all_files|flux_bootstrap_files|flux_system_files|authentik_blueprints|agentplane-staging:manifests|proxy_manifest' 51339f2e89e9c0693ccfdf10297afbd49a4e7333 -- \
  ':(glob)**/BUILD.bazel' ':(glob)**/BUILD' ':(glob)**/*.bzl' ':(exclude,glob)props/**'
```

I traced shared loaders and anchors through `cluster/validation/conftest.py`, `cluster/validation/cluster.py`, `cluster/cdk8s/manifest_roots.py`, and `util.bazel.runfiles.get_required_path`:

```sh
git grep -n -E '(_main/)?cluster/(k8s|generated)/|manifest_files\(|GENERATED_ROOT|generated_dir|k8s_dir|BLUEPRINTS_DIR_ANCHOR|proxy_manifest' 51339f2e89e9c0693ccfdf10297afbd49a4e7333 -- \
  cluster/validation cluster/cdk8s cluster/rotators agentplane/system_tests
```

The equivalent local scan for non-Python test files is:

```sh
git grep -n -E 'cluster/(k8s|generated)|manifest_files\(|k8s_dir|generated_dir|GENERATED_ROOT' 51339f2e89e9c0693ccfdf10297afbd49a4e7333 -- \
  ':(glob)**/*_test.go' ':(glob)**/*.test.ts' ':(glob)**/*.spec.ts' \
  ':(glob)**/*_test.ts' ':(glob)**/*_test.tsx' ':(glob)**/*test*.sh' ':(glob)**/*.bats' \
  ':(exclude,glob)props/**'
```

False positives were checked and excluded: `test_render_diff` creates temporary Git trees; `test_cluster`, `test_flux`, `test_checks`, `test_crd_layering`, `test_dependencies`, `test_health_checks`, and `test_sops_decryption` use temporary paths or synthetic resources. The Authentik and Forgejo rotator tests write to `tmp_path`. `test_image_registry` mentions `cluster/k8s/**` in a docstring while asserting literal URLs; `test_publisher` uses a Bazel target-name string; container E2E stages a fixture from `testdata/e2e_secrets`. `test_egress_fences` and Agentplane egress tests mention tree paths in comments or model metadata but synthesize in memory. The LiteLLM parser test uses explicit config fixtures; its production binary's manifest data is not a test data dependency.

## Recommended next directions

The ten current follow-up directions are:

1. **Split the two source-only checks from `test_cluster_integration`.** Move `test_files_flux_rewrites_use_block_style` and `test_flux_bootstrap_sources_need_no_decrypted_auth` to a focused target with only the authored inputs they inspect; they currently inherit the target's full generated-manifest data closure.
2. **Test Forgejo ImageRepository/Receiver wiring in the source model.** `test_image_automation_webhook_consistency` checks that every rendered GHCR ImageRepository is in the Receiver and that the Receiver names no missing repository. Add focused cdk8s coverage for this relation; the `forgejo_registry` package currently has no test target. Remove the whole-graph assertion only if the focused test covers both directions.
3. **Assess Forgejo image credential coverage.** Check whether focused cdk8s synthesis covers every workload and reflected pull Secret in `test_forgejo_image_namespaces_are_reflected`. Keep residual graph coverage for Helm or non-cdk8s workloads.
4. **Review external credential ownership coverage.** Compare `test_external_credential_ownership` with focused model tests; move only relations whose source and consumer are cdk8s-owned, keeping hand-written Secret and Flux boundary checks with the graph.
5. **Compare Goldilocks checks for overlap.** `test_goldilocks_namespace_labels` validates the rendered namespace set; compare it with namespace-constructor coverage and remove only proven duplication.
6. **Compare Cilium policy checks with synth-time validation.** `test_cilium_policy_rules_nonempty` also sees hand-written or post-Kustomize resources; retain that coverage unless the source-model tests cover those inputs.
7. **Review controller health and retry checks individually.** `test_controller_resources_have_health_checks` and `test_retry_policy` inspect Flux Kustomization specs and applied resources. Move an assertion only when source-model tests cover every relevant Kustomization.
8. **Keep image-policy markers with Flux source.** `test_image_policy_markers_resolve` depends on graph context to resolve markers to ImagePolicies; move it only with that source graph and its owner.
9. **Keep graph-structure checks with the graph owner.** `test_flux_build`, dependency resolution, orphan detection, SOPS decryption, and namespace-owner/delegation dependencies require the actual Flux graph.
10. **At cutover, remove only the parity comparison.** Keep generator invariants on in-memory output and move graph checks to the Flux source owner's pipeline.

Source-authored inputs such as blueprints, SOPS Secrets, SMTP config, and hand-written Kustomizations have their own source contracts. Tests for them should follow the source owner and read those inputs directly; Flux delivery location does not determine their test input.
