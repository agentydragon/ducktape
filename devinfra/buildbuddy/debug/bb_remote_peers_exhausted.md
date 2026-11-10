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

The upload-all comparison populated this shared CAS digest. A subsequent
minimal-mode build may therefore succeed while that blob remains available;
it still does not upload the launcher itself. Availability can depend on
another invocation having uploaded the same content, so a passing rerun alone
does not establish that the upload policy is correct.

## Integration and scope

`bbr` leaves Bazel's BEP upload mode unchanged. Add
`--remote_build_event_upload=all` explicitly to a remote build call that needs
locally generated regular files downloaded, as in the comparison above.
For local Gazelle validation, use `bb run //devinfra:gazelle -- -mode=diff`;
that builds the current generator and checks the local tree directly.
Only missing local BEP-referenced files are uploaded; existing remote outputs
are reused. A broad build can therefore add presence checks and uploads for
more local top-level files so the CLI can download them. Bazel offers no
per-file upload filter. This does not request every intermediate output or
change action-cache/RBE settings. The comparison added one 3923-byte blob
(2215 stored bytes); broad-build latency and storage overhead were not measured.

This establishes the fix for the observed regular launcher file. Upload-all
does not guarantee runfiles or declared directory contents. The
[upstream review](https://github.com/buildbuddy-io/buildbuddy/pull/12935#discussion_r3743154048)
identified those limits; the author
[removed upload-all](https://github.com/buildbuddy-io/buildbuddy/pull/12935#discussion_r3761949927)
and implemented the separate [runfile-symlink fix](https://github.com/buildbuddy-io/buildbuddy/pull/12962).
Current upstream `parseArgs` still only forces `--remote_upload_local_results`
for build-output fetching. A follow-up report should identify the remaining
build-only template-output case rather than claim a universal upload fix.

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

## Caller audit

The tracked remote build steps in `release-artifact/action.yml` (unstamped
and stamped), `nix-wheel-check.yml`, `push-images.yml`, and
`bench-remote-bazel.yml` received no upload-policy override. Released wheels
are `PyWheel` spawn outputs; the released Gazelle target is the compiled Go
binary, not the template-expanded `//devinfra:gazelle` launcher. OCI layouts
are directory outputs, which this flag does not cover.

The `agentplane/x/runner_layering/README.md` build recipe explicitly opts in
for `tools_rootfs` and `local_vm_probe`: the former exposes a Python module
written by `oci_layout_py`'s `ctx.actions.write`, and the latter exposes a
template-expanded Python launcher. The flag publishes their regular files;
the documented local `bb run` remains responsible for the probe's runfiles
and host execution. Archive, compiled-binary, library-validation, and
`--nobuild` recipes keep their existing settings.
