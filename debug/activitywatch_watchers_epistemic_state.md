# Epistemic State: ActivityWatch watcher expansion

## Last updated: 2026-07-11

## 1. Objective

Add reproducible ActivityWatch web and tmux collection to the Home Manager configuration, with a narrow Nix-owned Syncthing topology for ActivityWatch exports. Success means each retained collector has one clear lifecycle owner, writes to the local server, and does not duplicate processes across configuration reloads.

## 2. Available Action Space

- Package upstream tmux watcher source at a pinned commit with Nix.
- Run long-lived collectors as systemd user services.
- Force-install the official Chrome Web Store extension through NixOS Chrome policy.
- Inspect resulting ActivityWatch buckets and service logs after deployment.

## 3. Uncertainty Register

| ID  | Quantity                                            | Prior                                                      | Source                                                                  | Status                   |
| --- | --------------------------------------------------- | ---------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------ |
| U1  | `aw-watcher-input` coverage for SSH                 | None                                                       | SSH input arrives through a PTY, not the host's graphical input backend | Resolved; do not install |
| U2  | tmux watcher behavior across local and SSH sessions | One user tmux server exposes all of its sessions           | Upstream polls `tmux list-sessions`                                     | Resolved on wyrm2        |
| U3  | Chrome extension deployment                         | NixOS Chrome policy force-installs the Web Store extension | User preference                                                         | Resolved                 |

## 4. Hypothesis Space

| ID      | Hypothesis                                 | Probability | Evidence for                                   | Evidence against                       | Distinguishing test                      |
| ------- | ------------------------------------------ | ----------: | ---------------------------------------------- | -------------------------------------- | ---------------------------------------- |
| H1      | Tmux watcher observes the user tmux server |        0.99 | Live bucket events and active service on wyrm2 | Upstream is old and lightly maintained | Repeat after a tmux socket-layout change |
| H_other | Other behavior                             |        0.01 | Runtime details may differ on another host     | No current contrary evidence           | Live observation on another host         |

## 5. Evidence Log

- 2026-07-11, research lookup: official `aw-watcher-input` imports graphical keyboard and mouse listeners from `aw-watcher-afk`. It records local input counts, not SSH PTY activity, so it does not serve the requested non-graphical coverage.
- 2026-07-11, research lookup: `akohlbecker/aw-watcher-tmux` at commit `efaa7610add52bd2b39cd98d0e8e082b1e126487` polls the user's tmux server and reports session, window, pane title, current command, and current path.
- 2026-07-11, user preference: "is there a nix package? i'd be fine to make it machine level". NixOS `programs.chromium.extensions` writes Google Chrome managed policy, so the Chrome Web Store extension is force-installed without a separate local package.
- 2026-07-11, live observation: wyrm2's tmux server uses `/run/user/1001/tmux-1001/default`; setting `TMUX_TMPDIR=%t` in the user service aligned the watcher with that socket. The `aw-watcher-tmux` bucket then contained 17 events, including the active Codex pane.
- 2026-07-11, live observation: the pre-existing general Syncthing state was in `~/.config/syncthing`, while Home Manager expected `~/.local/state/syncthing`; its initializer therefore retried forever. The former was privately backed up and removed. The managed Syncthing instance now uses `--home=~/.local/state/syncthing`, has only the ActivityWatch folder and cluster peer, and completed initialization successfully.

## 6. Current Posterior State

- Web watcher: the official Chrome extension is force-installed by NixOS; no local daemon is required.
- Tmux watcher: the systemd service is active and reporting events through the user's runtime tmux socket.
- Input watcher: omit it; it duplicates some local activity signal from `awatcher` without covering SSH.
- Syncthing: the Nix-owned ActivityWatch-only instance is connected to `activitywatch-cluster`.

## 7. Action Queue

1. Verify Chrome installs the policy-managed extension and creates its bucket.
2. Roll out and verify the same configuration on the remaining ActivityWatch participants.

## 8. Decision Tree

- If another host uses a non-default tmux socket directory, retain the `%t`-based service environment rather than hard-coding wyrm2's path.
- Do not add privileged raw-input access merely to obtain input-intensity metrics.

## 9. Stopping Criteria

Stop once packages build, services are healthy, Syncthing is connected to the cluster receiver, and the expected web and SSH/tmux buckets exist.

## 10. Vibes Ledger

- Chrome's managed extension has not yet been observed creating its browser bucket; it requires a Chrome restart and browsing activity.
