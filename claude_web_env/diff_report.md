# Filesystem Diff Report

**live** vs **built**

## Summary

|                      | Count   | %        |
| -------------------- | ------- | -------- |
| Identical            | 120,952 | 19.4%    |
| Excluded (expected)  | 501,938 | 80.6%    |
| **Real differences** | **0**   | **0.0%** |
| Total                | 622,890 |          |

**Clean diff** (no unexpected differences)

## Excluded (expected differences)

- excluded: 464,003
- expected_only_left: 24,404
- expected_only_right: 12,908
- hash_excluded: 623

## Exclusion Pattern Utilization

116 patterns excluded 501,938 paths (501,938 attributed to specific patterns). 25 patterns matched 0 paths.

### `skip_paths` (33 patterns, 464,003 hits, 5 unused)

|    Hits | Pattern                              |
| ------: | ------------------------------------ |
| 308,178 | `/mnt`                               |
| 118,803 | `/root/.cache`                       |
|  17,937 | `/home/user/ducktape`                |
|   6,204 | `/root/.npm`                         |
|   6,136 | `/proc`                              |
|   2,884 | `/var/lib/dpkg/info`                 |
|   1,724 | `/root/.local/share/virtualenv`      |
|   1,255 | `/tmp`                               |
|     442 | `/usr/lib/debug/.build-id`           |
|     176 | `/sys`                               |
|     123 | `/home/claude/.npm`                  |
|      40 | `/run`                               |
|      30 | `/var/lib/apt/lists`                 |
|      19 | `/var/log`                           |
|      18 | `/dev`                               |
|       6 | `/root/.claude/projects`             |
|       5 | `/root/.claude/session-env`          |
|       4 | `/var/cache/apt`                     |
|       3 | `/root/.claude/debug`                |
|       3 | `/root/.claude/todos`                |
|       3 | `/var/lib/containers`                |
|       2 | `/home/claude/.claude/remote`        |
|       2 | `/home/claude/.ssh`                  |
|       2 | `/root/.claude/shell-snapshots`      |
|       1 | `/etc/docker`                        |
|       1 | `/home/claude/.cache`                |
|       1 | `/root/.claude/plans`                |
|       1 | `/var/tmp`                           |
|       0 | `/nix` **UNUSED**                    |
|       0 | `/root/.claude/plugins` **UNUSED**   |
|       0 | `/root/.claude/statsig` **UNUSED**   |
|       0 | `/root/.claude/telemetry` **UNUSED** |
|       0 | `/root/.local/share/pnpm` **UNUSED** |

### `volatile_paths` (44 patterns, 37,855 hits, 5 unused)

|   Hits | Pattern                                   |
| -----: | ----------------------------------------- |
| 22,093 | `/root/.local/share/uv/**`                |
|  8,701 | `/opt/ruby-*`                             |
|  2,903 | `/usr/local/lib/python*/**`               |
|  1,824 | `/opt/rbenv/**`                           |
|  1,242 | `/root/.local/lib/python*/**`             |
|    905 | `**/__pycache__/**`                       |
|     99 | `/opt/nvm/**`                             |
|     19 | `/var/cache/fontconfig/**`                |
|     17 | `/root/.rustup/**`                        |
|      8 | `/opt/node*/**`                           |
|      6 | `/root/.local/share/gem/**`               |
|      4 | `/var/cache/debconf/**`                   |
|      4 | `/var/lib/dpkg/alternatives/**`           |
|      3 | `/var/lib/postgresql/**`                  |
|      2 | `/root/.local/bin/*`                      |
|      2 | `/usr/local/use-go-*.sh`                  |
|      1 | `/etc/group`                              |
|      1 | `/etc/group-`                             |
|      1 | `/etc/gshadow`                            |
|      1 | `/etc/gshadow-`                           |
|      1 | `/etc/hostname`                           |
|      1 | `/etc/hosts`                              |
|      1 | `/etc/machine-id`                         |
|      1 | `/etc/passwd`                             |
|      1 | `/etc/passwd-`                            |
|      1 | `/etc/postgresql/**`                      |
|      1 | `/etc/shadow`                             |
|      1 | `/etc/shadow-`                            |
|      1 | `/etc/ssl/certs/java/cacerts`             |
|      1 | `/etc/ssl/certs/ssl-cert-snakeoil.pem`    |
|      1 | `/etc/ssl/private/ssl-cert-snakeoil.key`  |
|      1 | `/etc/sudoers`                            |
|      1 | `/root/.wget-hsts`                        |
|      1 | `/usr/local/bin/composer`                 |
|      1 | `/var/cache/ldconfig/**`                  |
|      1 | `/var/lib/apt/extended_states`            |
|      1 | `/var/lib/dbus/machine-id`                |
|      1 | `/var/lib/dpkg/status-old`                |
|      1 | `/var/lib/dpkg/triggers/**`               |
|      0 | `**/__pycache__` **UNUSED**               |
|      0 | `/usr/local/bin/golangci-lint` **UNUSED** |
|      0 | `/var/lib/dpkg/status` **UNUSED**         |
|      0 | `/var/lib/sgml-base/**` **UNUSED**        |
|      0 | `/var/lib/systemd/**` **UNUSED**          |

### `hash_may_differ` (1 patterns, 0 hits, 1 unused)

| Hits | Pattern                       |
| ---: | ----------------------------- |
|    0 | `/etc/ld.so.cache` **UNUSED** |

### `only_in_live` (27 patterns, 74 hits, 9 unused)

| Hits | Pattern                                               |
| ---: | ----------------------------------------------------- |
|   29 | `/root/.config/**`                                    |
|   18 | `/root/.gradle/**`                                    |
|    8 | `/root/.launchpadlib/**`                              |
|    5 | `/root/.claude.json.backup*`                          |
|    1 | `/.dockerenv`                                         |
|    1 | `/container_info.json`                                |
|    1 | `/etc/alternatives/python`                            |
|    1 | `/etc/apt/sources.list`                               |
|    1 | `/etc/apt/sources.list.d/ubuntu.sources`              |
|    1 | `/etc/containers/networks`                            |
|    1 | `/etc/ssl/certs/*.0`                                  |
|    1 | `/root/.bazelrc`                                      |
|    1 | `/root/.claude.json`                                  |
|    1 | `/root/.claude/stop-hook-git-check.sh`                |
|    1 | `/root/.gradle`                                       |
|    1 | `/root/.launchpadlib`                                 |
|    1 | `/usr/bin/python`                                     |
|    1 | `/var/lib/dpkg/alternatives/python`                   |
|    0 | `/etc/rc*.d/*docker` **UNUSED**                       |
|    0 | `/etc/systemd/system/*/containerd.service` **UNUSED** |
|    0 | `/etc/systemd/system/*/docker.*` **UNUSED**           |
|    0 | `/root/.claude/stats-cache.json` **UNUSED**           |
|    0 | `/root/.local/state` **UNUSED**                       |
|    0 | `/root/.local/state/**` **UNUSED**                    |
|    0 | `/var/cache/containers` **UNUSED**                    |
|    0 | `/var/cache/containers/**` **UNUSED**                 |
|    0 | `/var/lib/dpkg/alternatives/python3` **UNUSED**       |

### `session_hook_artifacts` (5 patterns, 0 hits, 5 unused)

| Hits | Pattern                                         |
| ---: | ----------------------------------------------- |
|    0 | `/etc/containers/containers.conf` **UNUSED**    |
|    0 | `/root/.nix-defexpr` **UNUSED**                 |
|    0 | `/root/.nix-defexpr/**` **UNUSED**              |
|    0 | `/root/.nix-profile` **UNUSED**                 |
|    0 | `/usr/local/bin/crun-gvisor-wrapper` **UNUSED** |

### `only_in_built` (6 patterns, 6 hits, 0 unused)

| Hits | Pattern                         |
| ---: | ------------------------------- |
|    1 | `/etc/apt/apt.conf.d/80retries` |
|    1 | `/etc/ssl/certs/*.0`            |
|    1 | `/usr/local/bin/conan`          |
|    1 | `/usr/local/bin/httpx`          |
|    1 | `/usr/local/bin/normalizer`     |
|    1 | `/usr/local/bin/websockets`     |
