# Filesystem Diff Report

**live** vs **built**

## Summary

|                      | Count   | %        |
| -------------------- | ------- | -------- |
| Identical            | 120,695 | 17.7%    |
| Excluded (expected)  | 562,197 | 82.3%    |
| **Real differences** | **5**   | **0.0%** |
| Total                | 682,897 |          |

## Real Differences

### Only in live (1)

**root-home** (1)

- `/root/.zshrc`

### Content changed (hash differs) (4)

**docs** (1)

- `/usr/share/doc/libpng16-16t64/changelog.Debian.gz` — size 2108->1501

**system-libs** (2)

- `/usr/lib/x86_64-linux-gnu/libpng16.a` — size 357068->355524
- `/usr/lib/x86_64-linux-gnu/libpng16.so.16.43.0` — size 223304->223304

**var** (1)

- `/var/lib/dpkg/status` — size 687887->687872

## Excluded (expected differences)

- excluded: 523,997
- expected_only_left: 24,588
- expected_only_right: 12,966
- hash_excluded: 646

## Exclusion Pattern Utilization

100 patterns excluded 562,197 paths (562,197 attributed to specific patterns). 7 patterns matched 0 paths.
Ratio: 20.0x patterns per real diff.

### `skip_paths` (28 patterns, 523,997 hits, 2 unused)

|    Hits | Pattern                                    |
| ------: | ------------------------------------------ |
| 359,092 | `/mnt`                                     |
| 124,049 | `/root/.cache`                             |
|  18,202 | `/home/user/ducktape`                      |
|  11,269 | `/proc`                                    |
|   6,292 | `/root/.npm`                               |
|   2,823 | `/var/lib/dpkg/info`                       |
|   1,246 | `/tmp`                                     |
|     442 | `/usr/lib/debug/.build-id`                 |
|     250 | `/sys`                                     |
|     123 | `/home/claude/.npm`                        |
|      77 | `/root/.claude/session-env`                |
|      46 | `/run`                                     |
|      26 | `/var/lib/apt/lists`                       |
|      19 | `/var/log`                                 |
|      18 | `/dev`                                     |
|       4 | `/var/cache/apt`                           |
|       3 | `/root/.claude/debug`                      |
|       3 | `/root/.claude/projects`                   |
|       3 | `/root/.claude/todos`                      |
|       2 | `/home/claude/.claude/remote`              |
|       2 | `/home/claude/.ssh`                        |
|       2 | `/root/.claude/shell-snapshots`            |
|       1 | `/etc/docker`                              |
|       1 | `/home/claude/.cache`                      |
|       1 | `/root/.claude/plans`                      |
|       1 | `/var/tmp`                                 |
|       0 | `/root/.local/share/virtualenv` **UNUSED** |
|       0 | `/var/lib/containers` **UNUSED**           |

### `volatile_paths` (39 patterns, 38,111 hits, 1 unused)

|   Hits | Pattern                                  |
| -----: | ---------------------------------------- |
| 22,113 | `/root/.local/share/uv/**`               |
|  8,701 | `/opt/ruby-*`                            |
|  2,911 | `/usr/local/lib/python*/**`              |
|  1,824 | `/opt/rbenv/**`                          |
|  1,242 | `/root/.local/lib/python*/**`            |
|    902 | `**/__pycache__/**`                      |
|    239 | `/opt/node*/**`                          |
|     99 | `/opt/nvm/**`                            |
|     19 | `/var/cache/fontconfig/**`               |
|     17 | `/root/.rustup/**`                       |
|      6 | `/root/.local/share/gem/**`              |
|      4 | `/var/cache/debconf/**`                  |
|      4 | `/var/lib/dpkg/alternatives/**`          |
|      3 | `/root/.local/bin/*`                     |
|      3 | `/var/lib/postgresql/**`                 |
|      2 | `/usr/local/use-go-*.sh`                 |
|      1 | `/etc/group`                             |
|      1 | `/etc/group-`                            |
|      1 | `/etc/gshadow`                           |
|      1 | `/etc/gshadow-`                          |
|      1 | `/etc/hosts`                             |
|      1 | `/etc/machine-id`                        |
|      1 | `/etc/passwd`                            |
|      1 | `/etc/passwd-`                           |
|      1 | `/etc/postgresql/**`                     |
|      1 | `/etc/shadow`                            |
|      1 | `/etc/shadow-`                           |
|      1 | `/etc/ssl/certs/java/cacerts`            |
|      1 | `/etc/ssl/certs/ssl-cert-snakeoil.pem`   |
|      1 | `/etc/ssl/private/ssl-cert-snakeoil.key` |
|      1 | `/etc/sudoers`                           |
|      1 | `/root/.wget-hsts`                       |
|      1 | `/usr/local/bin/composer`                |
|      1 | `/var/cache/ldconfig/**`                 |
|      1 | `/var/lib/apt/extended_states`           |
|      1 | `/var/lib/dbus/machine-id`               |
|      1 | `/var/lib/dpkg/status-old`               |
|      1 | `/var/lib/dpkg/triggers/**`              |
|      0 | `/etc/hostname` **UNUSED**               |

### `hash_may_differ` (3 patterns, 2 hits, 1 unused)

| Hits | Pattern                              |
| ---: | ------------------------------------ |
|    1 | `/etc/cloud/build.info`              |
|    1 | `/etc/ssl/certs/ca-certificates.crt` |
|    0 | `/etc/ld.so.cache` **UNUSED**        |

### `only_in_live` (24 patterns, 82 hits, 2 unused)

| Hits | Pattern                                     |
| ---: | ------------------------------------------- |
|   18 | `/root/.gradle/**`                          |
|   17 | `/root/.config/**`                          |
|   13 | `/root/.docker/**`                          |
|    8 | `/root/.launchpadlib/**`                    |
|    5 | `/root/.claude.json.backup*`                |
|    2 | `/etc/ssl/certs/*.[0-9]`                    |
|    2 | `/etc/ssl/certs/mkcert_*`                   |
|    2 | `/opt/containerd/**`                        |
|    2 | `/usr/local/share/ca-certificates/mkcert_*` |
|    1 | `/container_info.json`                      |
|    1 | `/etc/alternatives/python`                  |
|    1 | `/etc/apt/sources.list`                     |
|    1 | `/etc/apt/sources.list.d/ubuntu.sources`    |
|    1 | `/opt/containerd`                           |
|    1 | `/root/.bazelrc`                            |
|    1 | `/root/.claude.json`                        |
|    1 | `/root/.claude/stop-hook-git-check.sh`      |
|    1 | `/root/.docker`                             |
|    1 | `/root/.gradle`                             |
|    1 | `/root/.launchpadlib`                       |
|    1 | `/usr/bin/python`                           |
|    1 | `/var/lib/dpkg/alternatives/python`         |
|    0 | `/.dockerenv` **UNUSED**                    |
|    0 | `/etc/containers/networks` **UNUSED**       |

### `only_in_built` (6 patterns, 5 hits, 1 unused)

| Hits | Pattern                             |
| ---: | ----------------------------------- |
|    1 | `/etc/apt/apt.conf.d/80retries`     |
|    1 | `/usr/local/bin/conan`              |
|    1 | `/usr/local/bin/httpx`              |
|    1 | `/usr/local/bin/normalizer`         |
|    1 | `/usr/local/bin/websockets`         |
|    0 | `/etc/ssl/certs/*.[0-9]` **UNUSED** |
