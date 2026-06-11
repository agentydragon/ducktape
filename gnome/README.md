# gnome

Custom GNOME desktop utilities and Shell extensions for ducktape hosts.
Extensions are packaged via `nix/packages/gnome-shell-<name>.nix` and wired
into per-host home-manager config.

## Bazel-built distribution zip

Each extension exposes a `pkg_zip` target producing the standard
GNOME-extension distribution zip (extension files at the archive root, no
UUID-prefixed subdir). Same artifact the test container, the local
devkit launcher, and the Nix release pipeline consume:

```bash
bazelisk build //gnome/claude_quota:claude-quota_zip
# bazel-bin/gnome/claude_quota/claude-quota.zip
```

## Local iteration: nested devkit shell

`bazelisk run //gnome/claude_quota:devkit` builds the zip,
unpacks it into `~/.local/share/gnome-shell/extensions/<uuid>/`,
pre-enables the extension in dconf, and launches `gnome-shell --devkit
--wayland`. Requires `gnome-shell` on the host PATH.

To preview a specific render state (same fixture format as the golden
tests) without real auth/HTTP:

```bash
CLAUDE_QUOTA_FIXTURE=$PWD/gnome/claude_quota/test_fixtures/both_warn.json \
  bazelisk run //gnome/claude_quota:devkit
```

## Watch for errors

```bash
journalctl --user -f | grep -i "claude\|error\|extension"
```

## Enable in the running session

```bash
busctl --user call org.gnome.Shell /org/gnome/Shell \
    org.gnome.Shell.Extensions EnableExtension s "claude-quota@allegedly.works"
```

## Golden render tests

`//gnome/claude_quota:test_render` boots a real `gnome-shell`
inside a Bazel-built test container
(`//gnome/test_image:gnome_shell_test_image`; gnome-shell +
Xvfb + dbus + scrot pulled hermetically via `rules_distroless` apt),
unzips the distribution zip into the extension dir, launches one
gnome-shell for the whole module, and uses the extension's test DBus
interface to swap fixtures and toggle the popup menu between renders.
Each (fixture, view) pair is screenshotted with `scrot` and diff'd
against a checked-in golden via `util.testing.png_diff`.

Two views per fixture:

- **`panel_<fixture>`** — right-edge crop of the top panel (icons + pace labels).
- **`menu_<fixture>`** — popup menu open, cropped precisely to its actor
  bounding box (header rows, summary text, time/usage bars).

The fixture matrix exercises each branch of the renderer:

| Fixture     | What it covers                                                                                              |
| ----------- | ----------------------------------------------------------------------------------------------------------- |
| `both_ok`   | both providers in the ok band (deviation 0)                                                                 |
| `both_cool` | both providers under-running (deviation -15, cool/blue)                                                     |
| `both_warn` | both providers mildly over (deviation +10, warn/yellow)                                                     |
| `both_hot`  | both providers severely over (deviation +20, hot/red)                                                       |
| `short_hot` | short-window absolute-hot override (≥ 85% usage) wins binding tint while long-window pace label stays "+0%" |
| `mixed`     | per-provider tints don't bleed (Claude warn, Codex cool)                                                    |
| `error`     | error short-circuit (red icon + `!` label, "no data" rows in popup) on one provider                         |
| `no_data`   | initial state (both windows null → unknown tint, empty pace label, "no data" rows in popup)                 |

**Fixture state injection.** When `CLAUDE_QUOTA_FIXTURE` is set,
`extension.js` skips its HTTP/credential fetch path, loads
`{claude, codex}` provider state from the JSON file, and exports a
session-bus interface (`works.allegedly.ClaudeQuotaTest`) the test
driver uses for `Reload(path)`, `OpenMenu` / `CloseMenu`, and
`GetMenuGeometry` (returns the popup actor's screen-space bounding box
so the screenshot can be cropped precisely). Provide `resetSeconds`
directly (not `resetAtMs`) so rendering is independent of `Date.now()`.
Format:

```json
{
  "claude": {
    "short": { "usedPercent": 50, "resetSeconds": 9000, "windowSeconds": 18000 },
    "long": { "usedPercent": 40, "resetSeconds": 362880, "windowSeconds": 604800 },
    "lastFetch": null,
    "error": null
  },
  "codex": { ... }
}
```

**Updating goldens** when a rendering change is intentional:

```bash
# 1. Re-render every (fixture, view) with UPDATE_GOLDEN=1; PNGs land in
#    undeclared outputs as <view>_<fixture>.png.
bbr test //gnome/claude_quota:test_render \
  --test_env=UPDATE_GOLDEN=1 \
  --remote_download_outputs=toplevel --nocache_test_results

# 2. Pull each PNG from BuildBuddy into the source tree.
INV=$(cat ~/.cache/bbr/last_invocation_id)
for view in panel menu; do
  for f in both_ok both_cool both_warn both_hot \
           short_hot mixed error no_data; do
    bbapi artifact download "$INV" "${view}_${f}.png" \
      -o gnome/claude_quota/__snapshots__/${view}_${f}.png
  done
done

# 3. Eyeball, commit, then re-run without UPDATE_GOLDEN to confirm green.
bbr test //gnome/claude_quota:test_render
```

On comparison failure the test writes
`<view>_<fixture>.{actual,expected,diff}.png` and the gnome-shell log
to undeclared outputs for inspection.
