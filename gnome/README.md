# gnome

Custom GNOME desktop utilities and Shell extensions for ducktape hosts.
The AIQuota and Plaid Spend extension sources live in `aiquota/gnome/` and
`finance/plaid/spend/gnome/`; Bazel builds their ZIPs, and
`nix/packages/gnome-shell-aiquota.nix` and
`nix/packages/plaid-spend-desktop.nix` install the release artifacts through
Home Manager. `test_image/` is the GNOME Shell container used by their render
tests.

## Bazel-built distribution zip

The extension exposes a `pkg_zip` target producing the standard
GNOME-extension distribution zip (extension files at the archive root, no
UUID-prefixed subdir). Same artifact the test container, the local
devkit launcher, and the Nix release pipeline consume:

```bash
bazelisk build //aiquota/gnome:aiquota_zip
# bazel-bin/aiquota/gnome/aiquota.zip
```

## Local iteration: nested devkit shell

Run either extension in a nested shell using its Bazel-built ZIP:

```bash
bazelisk run //aiquota/gnome:devkit
bazelisk run //finance/plaid/spend/gnome:devkit
```

Both isolate dconf and the session D-Bus, and require `gnome-shell`, `unzip` and
`glib-compile-schemas` on the host. They refuse to start if a copy of the UUID
in `~/.local/share/gnome-shell/extensions/` would shadow the checkout. Close the
nested window to stop the shell and daemon and remove the temporary files.

Plaid starts with a synthetic allowance and pace history and no daemon, so the
panel settings can be previewed. Select another
fixture JSON with `-- --fixture /path/to/fixture.json` (the files in
`finance/plaid/spend/gnome/fixtures/` are examples). `-- --live` starts the
checkout's desktop daemon instead. Sign in from its menu: its refresh token
is kept in memory only, so it does not share credentials with the installed
daemon and requires a new login each time. Live sign-in uses the same fixed
loopback callback port as the installed client; concurrent login attempts cannot
share it. The daemon uses its normal API and OIDC defaults unless the
`PLAID_SPEND_API_URL`, `PLAID_SPEND_OIDC_ISSUER`, or
`PLAID_SPEND_OIDC_CLIENT_ID` environment variables are set.

Use `bazelisk run //finance/plaid/spend/gnome:devkit -- --host-daemon` to
connect the nested extension to the daemon already running in the host session.
It uses that daemon's current credentials and view; login from the preview also
operates on that daemon. The nested shell's settings remain isolated.

AIQuota starts its checkout daemon by default. To preview a view without real
auth/HTTP, provide the normal `/v1/quotas` view JSON generated from a
`aiquota/testing/fixtures/*.yaml` scenario using
`aiquota.testing.quota_fixtures.load_quota_view_fixture`:

```bash
AI_QUOTA_FIXTURE=/path/to/fixture.json bazelisk run //aiquota/gnome:devkit
# Or pass -- --fixture /path/to/fixture.json to either target.
```

## Watch for errors

```bash
journalctl --user -f | grep -i "claude\|error\|extension"
```

## Enable in the running session

```bash
busctl --user call org.gnome.Shell /org/gnome/Shell \
    org.gnome.Shell.Extensions EnableExtension s "aiquota@allegedly.works"
```

## Render tests

`//aiquota/gnome:test_render` exercises the extension in GNOME Shell and
publishes fixture screenshots for visual review. It checks render health; there
is no pixel golden or diff. Setup and capture details live in
`aiquota/gnome/test_render.py`.
