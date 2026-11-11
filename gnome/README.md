# gnome

Custom GNOME desktop utilities and Shell extensions for ducktape hosts.
Extensions are packaged via `nix/packages/gnome-shell-<name>.nix` and wired
into per-host home-manager config. The ai-quota extension is built from
`aiquota/gnome/`; `test_image/` here is the gnome-shell container its render test boots.

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

Plaid starts with a synthetic two-card fixture and no daemon. Select another
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

`//aiquota/gnome:test_render` boots a real `gnome-shell`
inside a Bazel-built test container
(`//gnome/test_image:gnome_shell_test_image`; gnome-shell +
Xvfb + dbus + scrot pulled hermetically via `rules_distroless` apt),
unzips the distribution zip into the extension dir, launches one
gnome-shell for the whole module, and uses the extension's test DBus
interface (`works.allegedly.AiQuotaTest`, exported when `AI_QUOTA_FIXTURE`
is set) to swap fixtures and toggle the popup menu between renders.
Each fixture in `aiquota/testing/fixtures/` is screenshotted with `scrot`
to one PNG (panel indicator plus open menu) that is published to the PR's
visual-review page.

There is no checked-in pixel golden and no pixel diff: the test passes when
gnome-shell boots, the extension reaches ENABLED, and every fixture opens its
menu and screenshots. Details: the docstring of `aiquota/gnome/test_render.py`.
