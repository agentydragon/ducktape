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

`bazelisk run //aiquota/gnome:devkit` builds the zip, unpacks it into an isolated
temp extension dir (the live `~/.local/share` and dconf are untouched, and it refuses
to run while `~/.local/share/gnome-shell/extensions/aiquota@allegedly.works` exists,
since that copy would shadow it), pre-enables the extension, and launches
`gnome-shell --devkit --wayland`. Requires `gnome-shell` on the host PATH.

To preview a specific render state without real auth/HTTP, point
`AI_QUOTA_FIXTURE` at the JSON form of one of the `aiquota/testing/fixtures/*.yaml`
fixtures (the render test converts them the same way):

```bash
AI_QUOTA_FIXTURE=/path/to/fixture.json bazelisk run //aiquota/gnome:devkit
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
