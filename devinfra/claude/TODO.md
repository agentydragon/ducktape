# Claude hook and statusline follow-ups

## Session setup and Bazel

- Consider making `devinfra/secrets/ci_env.sh` a fuller CI setup step for registry
  logins and release credentials after auditing their in-repo consumers.
- Revisit `DUCKTAPE_DOCKER_CLIENT_KEY` when Docker CI runs on RBE workers. Today
  `_common.sh` leaves it unset, and `devinfra/bbr.py` forwards
  `--remote_run_header` in a way inner Bazel flags cannot undo.
- Decide whether the `bb` shim should inject the session bazelrc. Check the
  behavior for local `bb build` / `bb test`, `bb remote`, and `bbr` before
  changing the wrapper.
- Consider defining the overlapping secret mappings once in Nix and generating
  an `activate-secrets` script for the standard user paths. Keep SSH and Attic
  secrets in sops-nix, and keep the session Bazelrc overlay separate because
  its proxy and JVM settings are session-local.

## Rust hook profile

- Decide whether connectivity diagnostics remain part of the hook contract;
  README currently says the Rust daemon does not implement them.
- `context_template` is present in the profiles and parsed by Rust, but Rust
  does not render it. Implement rendering or remove the unused setting and
  templates.
- Decide whether the Claude statusline should hide quota data already shown by
  the GNOME extension.

## Integration coverage

The container E2E test exercises the Rust hook in a hand-assembled image with
tools and a prepopulated Bazelisk cache. Add coverage for the Nix-packaged
`claude-hook` and the devtools `PATH` it uses, while keeping the binary under test
installed at test time. This should catch Nix runtime or PATH drift that the
container test and wheel import checks do not cover.

## Build tooling

Benchmark `bb remote` with and without `--config=rbe` on a nontrivial workload.
Compare execution time, action count, and cache hits before deciding whether
warm runner VMs make local execution worthwhile.
