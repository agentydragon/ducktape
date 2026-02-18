# Filesystem Diff Report

**live** vs **built**

## Summary

|                      | Count   | %        |
| -------------------- | ------- | -------- |
| Identical            | 120,691 | 19.5%    |
| Excluded (expected)  | 496,932 | 80.4%    |
| **Real differences** | **225** | **0.0%** |
| Total                | 617,848 |          |

## Real Differences

### Only in live (35)

**etc** (9)

- `/etc/ssl/certs/19b88685.1`
- `/etc/ssl/certs/19b88685.2`
- `/etc/ssl/certs/19b88685.3`
- `/etc/ssl/certs/19b88685.4`
- `/etc/ssl/certs/mkcert_development_CA_146294409348980041350021088509923286099.pem`
- `/etc/ssl/certs/mkcert_development_CA_159346910438027228767021915978872432122.pem`
- `/etc/ssl/certs/mkcert_development_CA_301561361566970237899705001524650534914.pem`
- `/etc/ssl/certs/mkcert_development_CA_91585158670217345857735022999250335775.pem`
- `/etc/ssl/certs/mkcert_development_CA_96398108628945581661820315379959453381.pem`

**opt-other** (3)

- `/opt/containerd`
- `/opt/containerd/bin`
- `/opt/containerd/lib`

**root-home** (18)

- `/root/.docker`
- `/root/.docker/.token_seed`
- `/root/.docker/.token_seed.lock`
- `/root/.docker/buildx`
- `/root/.docker/buildx/.buildNodeID`
- `/root/.docker/buildx/.lock`
- `/root/.docker/buildx/activity`
- `/root/.docker/buildx/activity/default`
- `/root/.docker/buildx/defaults`
- `/root/.docker/buildx/instances`
- `/root/.docker/buildx/refs`
- `/root/.docker/buildx/refs/default`
- `/root/.docker/buildx/refs/default/default`
- `/root/.docker/buildx/refs/default/default/750dglmf78rk07b2arel9yki8`
- `/root/.docker/buildx/refs/default/default/8fm92ali7ila1jt8h5u4yp9rt`
- `/root/.docker/buildx/refs/default/default/j0eqjgy4akqyskkqu8x9qcbww`
- `/root/.docker/buildx/refs/default/default/zlbu0kk5rnb8plj07kabrf6hb`
- `/root/.zshrc`

**usr-local** (5)

- `/usr/local/share/ca-certificates/mkcert_development_CA_146294409348980041350021088509923286099.crt`
- `/usr/local/share/ca-certificates/mkcert_development_CA_159346910438027228767021915978872432122.crt`
- `/usr/local/share/ca-certificates/mkcert_development_CA_301561361566970237899705001524650534914.crt`
- `/usr/local/share/ca-certificates/mkcert_development_CA_91585158670217345857735022999250335775.crt`
- `/usr/local/share/ca-certificates/mkcert_development_CA_96398108628945581661820315379959453381.crt`

### Only in built (183)

**docs** (63)

- `/usr/share/doc/aardvark-dns`
- `/usr/share/doc/aardvark-dns/changelog.Debian.gz`
- `/usr/share/doc/aardvark-dns/copyright`
- `/usr/share/doc/buildah`
- `/usr/share/doc/buildah/changelog.Debian.gz`
- `/usr/share/doc/buildah/copyright`
- `/usr/share/doc/buildah/examples`
- `/usr/share/doc/buildah/examples/cni-examples`
- `/usr/share/doc/buildah/release-announcements`
- `/usr/share/doc/buildah/tutorials`
- `/usr/share/doc/catatonit`
- `/usr/share/doc/catatonit/changelog.Debian.gz`
- `/usr/share/doc/catatonit/copyright`
- `/usr/share/doc/conmon`
- `/usr/share/doc/conmon/changelog.Debian.gz`
- `/usr/share/doc/conmon/copyright`
- `/usr/share/doc/containernetworking-plugins`
- `/usr/share/doc/containernetworking-plugins/changelog.Debian.gz`
- `/usr/share/doc/containernetworking-plugins/copyright`
- `/usr/share/doc/crun`
- `/usr/share/doc/crun/changelog.Debian.gz`
- `/usr/share/doc/crun/copyright`
- `/usr/share/doc/fuse-overlayfs`
- `/usr/share/doc/fuse-overlayfs/changelog.Debian.gz`
- `/usr/share/doc/fuse-overlayfs/copyright`
- `/usr/share/doc/fuse3`
- `/usr/share/doc/fuse3/changelog.Debian.gz`
- `/usr/share/doc/fuse3/copyright`
- `/usr/share/doc/golang-github-containers-common`
- `/usr/share/doc/golang-github-containers-common/changelog.Debian.gz`
- `/usr/share/doc/golang-github-containers-common/copyright`
- `/usr/share/doc/golang-github-containers-image`
- `/usr/share/doc/golang-github-containers-image/changelog.Debian.gz`
- `/usr/share/doc/golang-github-containers-image/copyright`
- `/usr/share/doc/golang-github-containers-image/examples`
- `/usr/share/doc/libfuse3-3`
- `/usr/share/doc/libfuse3-3/changelog.Debian.gz`
- `/usr/share/doc/libfuse3-3/copyright`
- `/usr/share/doc/libslirp0`
- `/usr/share/doc/libslirp0/changelog.Debian.gz`
- `/usr/share/doc/libslirp0/copyright`
- `/usr/share/doc/libsubid4`
- `/usr/share/doc/libsubid4/changelog.Debian.gz`
- `/usr/share/doc/libsubid4/copyright`
- `/usr/share/doc/netavark`
- `/usr/share/doc/netavark/changelog.Debian.gz`
- `/usr/share/doc/netavark/copyright`
- `/usr/share/doc/passt`
- `/usr/share/doc/passt/changelog.Debian.gz`
- `/usr/share/doc/passt/copyright`
- `/usr/share/doc/podman`
- `/usr/share/doc/podman/changelog.Debian.gz`
- `/usr/share/doc/podman/copyright`
- `/usr/share/doc/podman/examples`
- `/usr/share/doc/podman/examples/cni`
- `/usr/share/doc/podman/examples/cni/net.d`
- `/usr/share/doc/slirp4netns`
- `/usr/share/doc/slirp4netns/changelog.Debian.gz`
- `/usr/share/doc/slirp4netns/copyright`
- `/usr/share/doc/uidmap`
- `/usr/share/doc/uidmap/NEWS.Debian.gz`
- `/usr/share/doc/uidmap/changelog.Debian.gz`
- `/usr/share/doc/uidmap/copyright`

**etc** (24)

- `/etc/apparmor.d/abstractions`
- `/etc/apparmor.d/abstractions/passt`
- `/etc/apparmor.d/abstractions/pasta`
- `/etc/apparmor.d/local/usr.bin.passt`
- `/etc/apparmor.d/usr.bin.passt`
- `/etc/cni`
- `/etc/cni/net.d`
- `/etc/cni/net.d/87-podman-bridge.conflist`
- `/etc/containers`
- `/etc/containers/libpod.conf`
- `/etc/containers/policy.json`
- `/etc/containers/registries.conf`
- `/etc/containers/registries.conf.d`
- `/etc/containers/registries.conf.d/shortnames.conf`
- `/etc/containers/systemd`
- `/etc/containers/systemd/users`
- `/etc/fuse.conf`
- `/etc/systemd/system/default.target.wants`
- `/etc/systemd/system/default.target.wants/podman-auto-update.service`
- `/etc/systemd/system/default.target.wants/podman-clean-transient.service`
- `/etc/systemd/system/default.target.wants/podman-restart.service`
- `/etc/systemd/system/default.target.wants/podman.service`
- `/etc/systemd/system/sockets.target.wants/podman.socket`
- `/etc/systemd/system/timers.target.wants/podman-auto-update.timer`

**other** (43)

- `/sbin.usr-is-merged`
- `/usr/lib/cni`
- `/usr/lib/cni/bandwidth`
- `/usr/lib/cni/bridge`
- `/usr/lib/cni/dhcp`
- `/usr/lib/cni/firewall`
- `/usr/lib/cni/host-device`
- `/usr/lib/cni/host-local`
- `/usr/lib/cni/ipvlan`
- `/usr/lib/cni/loopback`
- `/usr/lib/cni/macvlan`
- `/usr/lib/cni/portmap`
- `/usr/lib/cni/ptp`
- `/usr/lib/cni/sbr`
- `/usr/lib/cni/static`
- `/usr/lib/cni/tuning`
- `/usr/lib/cni/vlan`
- `/usr/lib/cni/vrf`
- `/usr/lib/podman`
- `/usr/lib/podman/aardvark-dns`
- `/usr/lib/podman/netavark`
- `/usr/lib/systemd/system-generators/podman-system-generator`
- `/usr/lib/systemd/system/cni-dhcp.service`
- `/usr/lib/systemd/system/cni-dhcp.socket`
- `/usr/lib/systemd/system/podman-auto-update.service`
- `/usr/lib/systemd/system/podman-auto-update.timer`
- `/usr/lib/systemd/system/podman-clean-transient.service`
- `/usr/lib/systemd/system/podman-kube@.service`
- `/usr/lib/systemd/system/podman-restart.service`
- `/usr/lib/systemd/system/podman.service`
- `/usr/lib/systemd/system/podman.socket`
- `/usr/lib/systemd/user-generators/podman-user-generator`
- `/usr/lib/systemd/user/podman-auto-update.service`
- `/usr/lib/systemd/user/podman-auto-update.timer`
- `/usr/lib/systemd/user/podman-kube@.service`
- `/usr/lib/systemd/user/podman-restart.service`
- `/usr/lib/systemd/user/podman.service`
- `/usr/lib/systemd/user/podman.socket`
- `/usr/lib/tmpfiles.d/podman.conf`
- `/usr/libexec/podman`
- `/usr/libexec/podman/catatonit`
- `/usr/libexec/podman/quadlet`
- `/usr/libexec/podman/rootlessport`

**system-binaries** (20)

- `/usr/bin/buildah`
- `/usr/bin/catatonit`
- `/usr/bin/conmon`
- `/usr/bin/crun`
- `/usr/bin/fuse-overlayfs`
- `/usr/bin/fusermount`
- `/usr/bin/fusermount3`
- `/usr/bin/getsubids`
- `/usr/bin/newgidmap`
- `/usr/bin/newuidmap`
- `/usr/bin/passt`
- `/usr/bin/passt.avx2`
- `/usr/bin/pasta`
- `/usr/bin/pasta.avx2`
- `/usr/bin/podman`
- `/usr/bin/podmansh`
- `/usr/bin/qrap`
- `/usr/bin/slirp4netns`
- `/usr/sbin/mount.fuse`
- `/usr/sbin/mount.fuse3`

**system-libs** (7)

- `/usr/lib/x86_64-linux-gnu/libcrun.a`
- `/usr/lib/x86_64-linux-gnu/libfuse3.so.3`
- `/usr/lib/x86_64-linux-gnu/libfuse3.so.3.14.0`
- `/usr/lib/x86_64-linux-gnu/libslirp.so.0`
- `/usr/lib/x86_64-linux-gnu/libslirp.so.0.4.0`
- `/usr/lib/x86_64-linux-gnu/libsubid.so.4`
- `/usr/lib/x86_64-linux-gnu/libsubid.so.4.0.0`

**usr-share** (11)

- `/usr/share/bash-completion/completions/buildah`
- `/usr/share/bash-completion/completions/podman`
- `/usr/share/containers`
- `/usr/share/containers/containers.conf`
- `/usr/share/containers/seccomp.json`
- `/usr/share/initramfs-tools`
- `/usr/share/initramfs-tools/hooks`
- `/usr/share/initramfs-tools/hooks/fuse`
- `/usr/share/lintian/overrides/catatonit`
- `/usr/share/lintian/overrides/uidmap`
- `/usr/share/zsh/vendor-completions/_podman`

**var** (15)

- `/var/lib/systemd/deb-systemd-helper-enabled/cni-dhcp.service.dsh-also`
- `/var/lib/systemd/deb-systemd-helper-enabled/cni-dhcp.socket.dsh-also`
- `/var/lib/systemd/deb-systemd-helper-enabled/default.target.wants`
- `/var/lib/systemd/deb-systemd-helper-enabled/default.target.wants/podman-auto-update.service`
- `/var/lib/systemd/deb-systemd-helper-enabled/default.target.wants/podman-clean-transient.service`
- `/var/lib/systemd/deb-systemd-helper-enabled/default.target.wants/podman-restart.service`
- `/var/lib/systemd/deb-systemd-helper-enabled/default.target.wants/podman.service`
- `/var/lib/systemd/deb-systemd-helper-enabled/podman-auto-update.service.dsh-also`
- `/var/lib/systemd/deb-systemd-helper-enabled/podman-auto-update.timer.dsh-also`
- `/var/lib/systemd/deb-systemd-helper-enabled/podman-clean-transient.service.dsh-also`
- `/var/lib/systemd/deb-systemd-helper-enabled/podman-restart.service.dsh-also`
- `/var/lib/systemd/deb-systemd-helper-enabled/podman.service.dsh-also`
- `/var/lib/systemd/deb-systemd-helper-enabled/podman.socket.dsh-also`
- `/var/lib/systemd/deb-systemd-helper-enabled/sockets.target.wants/podman.socket`
- `/var/lib/systemd/deb-systemd-helper-enabled/timers.target.wants/podman-auto-update.timer`

### Content changed (hash differs) (7)

**docs** (1)

- `/usr/share/doc/libpng16-16t64/changelog.Debian.gz` — size 2108->1501

**etc** (2)

- `/etc/cloud/build.info` — size 50->52
- `/etc/ssl/certs/ca-certificates.crt` — size 229964->221954

**system-libs** (2)

- `/usr/lib/x86_64-linux-gnu/libpng16.a` — size 357068->355524
- `/usr/lib/x86_64-linux-gnu/libpng16.so.16.43.0` — size 223304->223304

**usr-local** (1)

- `/usr/local/bin/environment-manager` — size 26689073->26621418

**var** (1)

- `/var/lib/dpkg/status` — size 687887->716838

## Excluded (expected differences)

- excluded: 458,753
- expected_only_left: 24,566
- expected_only_right: 12,966
- hash_excluded: 647

## Exclusion Pattern Utilization

92 patterns excluded 496,932 paths (496,932 attributed to specific patterns). 6 patterns matched 0 paths.
Ratio: 0.4x patterns per real diff.

### `skip_paths` (28 patterns, 458,753 hits, 2 unused)

|    Hits | Pattern                                    |
| ------: | ------------------------------------------ |
| 293,241 | `/mnt`                                     |
| 124,050 | `/root/.cache`                             |
|  18,200 | `/home/user/ducktape`                      |
|  11,665 | `/proc`                                    |
|   6,295 | `/root/.npm`                               |
|   2,884 | `/var/lib/dpkg/info`                       |
|   1,261 | `/tmp`                                     |
|     442 | `/usr/lib/debug/.build-id`                 |
|     250 | `/sys`                                     |
|     191 | `/root/.claude/session-env`                |
|     123 | `/home/claude/.npm`                        |
|      52 | `/run`                                     |
|      26 | `/var/lib/apt/lists`                       |
|      19 | `/var/log`                                 |
|      18 | `/dev`                                     |
|       9 | `/root/.claude/projects`                   |
|       9 | `/root/.claude/todos`                      |
|       4 | `/var/cache/apt`                           |
|       3 | `/root/.claude/debug`                      |
|       3 | `/root/.claude/shell-snapshots`            |
|       2 | `/home/claude/.claude/remote`              |
|       2 | `/home/claude/.ssh`                        |
|       1 | `/etc/docker`                              |
|       1 | `/home/claude/.cache`                      |
|       1 | `/root/.claude/plans`                      |
|       1 | `/var/tmp`                                 |
|       0 | `/root/.local/share/virtualenv` **UNUSED** |
|       0 | `/var/lib/containers` **UNUSED**           |

### `volatile_paths` (39 patterns, 38,113 hits, 1 unused)

|   Hits | Pattern                                  |
| -----: | ---------------------------------------- |
| 22,113 | `/root/.local/share/uv/**`               |
|  8,701 | `/opt/ruby-*`                            |
|  2,911 | `/usr/local/lib/python*/**`              |
|  1,824 | `/opt/rbenv/**`                          |
|  1,242 | `/root/.local/lib/python*/**`            |
|    904 | `**/__pycache__/**`                      |
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

### `hash_may_differ` (1 patterns, 1 hits, 0 unused)

| Hits | Pattern            |
| ---: | ------------------ |
|    1 | `/etc/ld.so.cache` |

### `only_in_live` (18 patterns, 60 hits, 2 unused)

| Hits | Pattern                                  |
| ---: | ---------------------------------------- |
|   18 | `/root/.gradle/**`                       |
|   17 | `/root/.config/**`                       |
|    8 | `/root/.launchpadlib/**`                 |
|    5 | `/root/.claude.json.backup*`             |
|    1 | `/container_info.json`                   |
|    1 | `/etc/alternatives/python`               |
|    1 | `/etc/apt/sources.list`                  |
|    1 | `/etc/apt/sources.list.d/ubuntu.sources` |
|    1 | `/etc/ssl/certs/*.0`                     |
|    1 | `/root/.bazelrc`                         |
|    1 | `/root/.claude.json`                     |
|    1 | `/root/.claude/stop-hook-git-check.sh`   |
|    1 | `/root/.gradle`                          |
|    1 | `/root/.launchpadlib`                    |
|    1 | `/usr/bin/python`                        |
|    1 | `/var/lib/dpkg/alternatives/python`      |
|    0 | `/.dockerenv` **UNUSED**                 |
|    0 | `/etc/containers/networks` **UNUSED**    |

### `only_in_built` (6 patterns, 5 hits, 1 unused)

| Hits | Pattern                         |
| ---: | ------------------------------- |
|    1 | `/etc/apt/apt.conf.d/80retries` |
|    1 | `/usr/local/bin/conan`          |
|    1 | `/usr/local/bin/httpx`          |
|    1 | `/usr/local/bin/normalizer`     |
|    1 | `/usr/local/bin/websockets`     |
|    0 | `/etc/ssl/certs/*.0` **UNUSED** |
