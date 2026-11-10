# `bb remote build` missing local outputs

Verified 2026-11-10 with `bb` 5.0.500 and Bazel 9.2.0.

`bb remote build //devinfra:gazelle` can finish the hosted Bazel build
successfully, then exit 1 downloading a generated launcher from CAS:

```text
download blob: read c755c56d73fe520d31a41295a6dacdfaef93369e2f1605c311d27e1f088e6cb0/3923:
rpc error: code = NotFound desc = Exhausted all peers attempting to read
"c755c56d73fe520d31a41295a6dacdfaef93369e2f1605c311d27e1f088e6cb0".
```

## Proven mechanism

Bazel's `--remote_build_event_upload` defaults to `minimal`. That mode skips
uploading local build outputs other than logs/profiles, yet emits their
`bytestream://` URIs even when the blobs are absent. See Bazel 9.2.0's
[`RemoteOptions`](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/remote/options/RemoteOptions.java#L314-L328)
and [`ByteStreamBuildEventArtifactUploader`](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/remote/ByteStreamBuildEventArtifactUploader.java#L238-L247)
(`shouldUpload` and `PathConverterImpl`).

Gazelle's `gazelle-runner.bash` is produced by a local `TemplateExpand`
internal action, not a remotely executed spawn. The failing build's Bazel
profile records that expansion in 885 microseconds. Both top-level outputs,
`gazelle-runner.bash` and `gazelle`, advertise the same 3923-byte digest.

The CLI enables `--remote_upload_local_results`, which covers locally
executed spawn results but does not upload this internal template output.
After the runner exits, the CLI reads the target's BEP output URIs and tries
to download both files. See
[`parseArgs` and `downloadOutputs`](https://github.com/buildbuddy-io/buildbuddy/blob/0ca572258f0eecd479a7a4df644ccdbae4c94cca/cli/remotebazel/remotebazel.go)
(source at the CLI release date; hosted-service revision is independent).

## Controlled comparison

Both builds used devel commit `4cf722d394f39b8d65960d0d622806de6c71d05f`,
with the repository's RBE/cache settings enabled. Only the BEP upload flag
changed. Commands run from an isolated worktree:

```bash
bbr build //devinfra:gazelle
bbr build //devinfra:gazelle --remote_build_event_upload=all
```

| Invocation                                                                                             | Hosted Bazel | Local CLI | Launcher                |
| ------------------------------------------------------------------------------------------------------ | ------------ | --------- | ----------------------- |
| [Original PR #15300 build](https://app.buildbuddy.io/invocation/2ac0667c-54d7-4259-a9ca-e8a52ce0e340)  | Success      | Exit 1    | CAS NotFound            |
| [Default-mode reproduction](https://app.buildbuddy.io/invocation/5a5357ba-e90d-4159-82c9-79606704af3c) | Success      | Exit 1    | Same CAS NotFound       |
| [Upload-all comparison](https://app.buildbuddy.io/invocation/be41e8a3-522e-4bd6-9e20-ee399235888f)     | Success      | Exit 0    | Both outputs downloaded |

Before the comparison, `bbapi cache metadata` returned NotFound for the
launcher, and the artifact HTTP endpoint returned 404. The original build's
159948-byte Bazel profile downloaded successfully as a control.

After the upload-all build, metadata reported a 3923-byte blob modified at
2026-11-10 20:57:56.585888 UTC. An independent `bbapi artifact download`
returned 3923 bytes whose SHA-256 was exactly
`c755c56d73fe520d31a41295a6dacdfaef93369e2f1605c311d27e1f088e6cb0`.
The CLI also materialized both files under
`bb-out/bazel-out/k8-fastbuild/bin/devinfra/`.

## Integration and scope

`bbr build` supplies `--remote_build_event_upload=all` before session and
user options. Direct `bb remote build` callers must pass the flag explicitly.
A later explicit `minimal` override restores Bazel's original behavior.
Only missing local BEP-referenced files are uploaded; existing remote outputs
are reused. A broad build can therefore add presence checks and uploads for
more local top-level files so the CLI can download them. Bazel offers no
per-file upload filter. This does not request every intermediate output or
change action-cache/RBE settings. The comparison added one 3923-byte blob
(2215 stored bytes); broad-build latency and storage overhead were not measured.

`bb remote test` does not download build outputs. CI's `--script` path also
avoids this post-run download. Local `bb run` can use the launcher directly
without retrieving it from CAS. The CLI's build-remotely/run-locally path
separately uploads its executable and runfiles.

An earlier 2026-05 investigation recorded similar errors for launcher
`71eb57bd65aa8516fcc15da1aee0cb71ca9993c92a2796b0b516c2c8f2ee2fe2/3813`,
runner stderr, and `vm_log_tail.txt`, and proposed distributed-cache loss
associated with `DoNotCache`. Its reference invocation was
`bf3aafbf-fcab-4d72-9509-57d6b4abbb68`, child
`6b93e3de-51f9-4a46-98ed-a27db464ab3f`. Those historical diagnostic blobs were
not re-investigated here; the current controlled result establishes missing
BEP uploads for the Gazelle launcher and does not establish their cause.
A broad recent-invocation query timed out, and target history contained no
Gazelle entries, so these observations are not a population failure rate.
